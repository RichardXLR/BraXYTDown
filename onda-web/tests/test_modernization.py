"""Functional failure, resource, isolation and durable-worker regressions."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import urllib.request
import pytest
from fastapi.testclient import TestClient
from api import engine,index,jobs,security
from api.jobs import JobStore
from api.operations import admit,cached_metadata,metadata_key
from api.resources import ResourcePolicy,register_workspace,release_workspace
from api.worker import process_job


def test_resource_policy_serverless_cannot_enable_worker_or_extend_deadline():
    policy=ResourcePolicy.from_environment({'VERCEL':'1','ONDA_WORKER_MODE':'true','ONDA_MAX_WORKSPACE_BYTES':str(10**12),'ONDA_PROCESS_SECONDS':'86400','ONDA_TOTAL_SECONDS':'86400'})
    assert policy.mode=='inline' and policy.max_workspace_bytes==448*1024**2
    assert policy.process_seconds<=240 and policy.total_seconds<=290


def test_shared_workspace_budget_counts_other_active_tasks(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'
    a.mkdir();b.mkdir();(a/'input').write_bytes(b'a'*100);(b/'input').write_bytes(b'b'*100)
    policy=ResourcePolicy(max_workspace_bytes=250,reserve_bytes=0)
    register_workspace(a);register_workspace(b)
    try:
        assert policy.workspace_available(a)==50
        with pytest.raises(security.AudioError,match='espaço'):
            policy.check_workspace(b,51)
    finally:
        release_workspace(a);release_workspace(b)


def test_separate_transfer_and_output_budgets_accept_250mb():
    guard=security.Guard(resource_policy=ResourcePolicy())
    guard.count(250*1024**2);guard.check_output(250*1024**2)
    guard.count(250*1024**2)
    assert guard.received==500*1024**2


def test_bounded_dns_deadline_does_not_wait_for_blocking_resolver(monkeypatch):
    finish=threading.Event()
    def blocked(*args,**kwargs):
        finish.wait(2)
        return [(2,1,6,'',('8.8.8.8',443))]
    monkeypatch.setattr(security.socket,'getaddrinfo',blocked)
    started=time.monotonic()
    try:
        with pytest.raises(security.AudioError):
            security.public_url('https://public.example/video.mp4',guard=security.Guard(seconds=.03))
        assert time.monotonic()-started<.25
    finally:
        finish.set()


@pytest.mark.parametrize('destination',['https://other.example/login','http://public.example/login'])
def test_redirect_never_forwards_secret_post_body(monkeypatch,destination):
    monkeypatch.setattr(security,'public_url',lambda *a,**kw:('public.example',[]))
    request=urllib.request.Request('https://public.example/login',data=b'password=private',method='POST')
    with pytest.raises(security.AudioError) as error:
        security.PublicRedirectHandler(security.Guard()).redirect_request(request,None,307,'Redirect',{},destination)
    assert error.value.code=='unsafe_redirect'


def test_metadata_cache_copies_public_results_and_skips_sessions():
    body=index.LinkInput(url='https://www.youtube.com/watch?v=owned_fixture')
    key=metadata_key(body)
    cached_metadata(key,{'title':'Own fixture','formats':[]})
    returned=cached_metadata(key);returned['formats'].append('mutation')
    assert cached_metadata(key)['formats']==[]
    assert metadata_key(index.LinkInput(url='https://public.example/file.mp4?token=private')) is None
    assert metadata_key(index.LinkInput(url=body.url,cookies='private')) is None


def test_inline_admission_is_per_account_and_idempotent():
    def request(user,key):
        return SimpleNamespace(headers={'idempotency-key':key},state=SimpleNamespace(onda_auth=SimpleNamespace(user_id=user)))
    body=index.DownloadInput(url='https://public.example/own.wav')
    a,release_a=admit(request('user_a','a'*32),body)
    b,release_b=admit(request('user_b','a'*32),body)
    try:
        assert a!=b
        with pytest.raises(security.AudioError) as error:
            admit(request('user_a','a'*32),body)
        assert error.value.code=='duplicate_download'
        with pytest.raises(security.AudioError) as error:
            admit(request('user_a','b'*32),body)
        assert error.value.code=='user_busy'
    finally:
        release_a();release_b()


def test_asgi_deadline_aborts_slow_delivery_and_cleans_resources():
    cleaned=[]
    async def content():
        yield b'fixture'
        await asyncio.sleep(2)
    async def send(_message):
        await asyncio.sleep(2)
    async def receive():
        await asyncio.sleep(2)
        return {'type':'http.disconnect'}
    async def scenario():
        response=index.DownloadResponse(content(),cleanup=lambda:cleaned.append(True),deadline=time.monotonic()+.03)
        with pytest.raises(asyncio.TimeoutError):
            await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
    asyncio.run(scenario())
    assert cleaned==[True]


@pytest.fixture
def store():
    import tempfile
    root=Path(__file__).resolve().parents[1]/'artifacts'/'test-jobs'
    root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield JobStore(directory,b'x'*32)


def test_job_payload_is_encrypted_and_isolated(store):
    payload={'cookies':'secret-cookie-value','url':'https://public.example/own.wav'}
    a=store.owner('user_a');b=store.owner('user_b')
    job,created=store.create(a,'same-request',payload)
    again,duplicate=store.create(a,'same-request',payload)
    other,_=store.create(b,'same-request',payload)
    assert created and not duplicate and again['id']==job['id'] and other['id']!=job['id']
    assert b'secret-cookie-value' not in store.database.read_bytes()
    assert store.open(job['id'],store.get(job['id'],a)['payload'])==payload
    with pytest.raises(security.AudioError):
        store.get(job['id'],b)
    with pytest.raises(security.AudioError):
        store.create(a,'same-request',{'url':'https://other.example/own.wav'})
    assert 'cookies' not in json.dumps(store.snapshot(store.get(job['id'])))


def test_cancelled_job_cannot_be_resurrected_by_old_worker(store):
    owner=store.owner('user_a');job,_=store.create(owner,'request',{'url':'https://public.example/own.wav'})
    claim=store.claim();store.cancel(job['id'],owner)
    assert not store.update(job['id'],claim['fence'],status='ready',file={'filename':'a.wav'})
    row=store.get(job['id'])
    assert row['status']=='cancelled' and row['payload'] is None


def test_restart_reclaims_expired_lease_with_new_fence(store):
    job,_=store.create(store.owner('user_a'),'request',{'url':'https://public.example/own.wav'})
    old=store.claim()
    store.update(job['id'],old['fence'],checkpoint={'kind':'acquired','inputs':[],'details':{}})
    with store.connect() as db:
        db.execute('UPDATE jobs SET lease=0 WHERE id=?',(job['id'],))
    new=store.claim()
    assert new['fence']!=old['fence'] and new['attempts']==2 and new['checkpoint']
    assert not store.update(job['id'],old['fence'],status='ready')


def test_worker_uses_retained_verified_input_after_restart(store,monkeypatch):
    monkeypatch.setenv('ONDA_WORKER_MODE','true')
    payload=index.DownloadInput(url='https://public.example/own.wav',format='wav',quality='source').model_dump()
    job,_=store.create(store.owner('user_fixture'),'request',payload)
    directory=store.root/job['id'];directory.mkdir()
    import shutil
    source=directory/'source.media';shutil.copyfile(Path('public/canary.wav'),source)
    old=store.claim()
    checkpoint={'kind':'acquired','video':False,'inputs':[{'path':'source.media','sha256':hashlib.sha256(source.read_bytes()).hexdigest()}],'details':{'title':'Own fixture'}}
    store.update(job['id'],old['fence'],checkpoint=checkpoint)
    with store.connect() as db:
        db.execute('UPDATE jobs SET lease=0 WHERE id=?',(job['id'],))
    monkeypatch.setattr(engine,'acquire_with_recovery',lambda *a,**kw:pytest.fail('completed input was downloaded again'))
    process_job(store,store.claim())
    result=store.get(job['id'])
    assert result['status']=='ready' and result['payload'] is None and result['checkpoint'] is None
    file=json.loads(result['file']);target=directory/file['path']
    assert hashlib.sha256(target.read_bytes()).hexdigest()==file['sha256']
    assert file['size']==target.stat().st_size>0


def test_job_delivery_is_owned_signed_and_supports_ranges(store,monkeypatch):
    monkeypatch.setenv('ONDA_WORKER_MODE','true');monkeypatch.setattr(jobs,'_store',store)
    owner=store.owner('user_fixture');job,_=store.create(owner,'request',{'url':'https://public.example/own.wav'})
    claim=store.claim();directory=store.root/job['id'];directory.mkdir();target=directory/'audio.wav';target.write_bytes(b'abcdef')
    digest=hashlib.sha256(b'abcdef').hexdigest()
    store.update(job['id'],claim['fence'],status='ready',file={'path':'audio.wav','filename':'Own.wav','size':6,'mimeType':'audio/wav','sha256':digest,'etag':'"'+digest+'"'})
    client=TestClient(index.app)
    delivery=client.post(f'/api/jobs/{job["id"]}/delivery').json()
    url=delivery['url'];response=client.get(url,headers={'Range':'bytes=1-3'})
    assert response.status_code==206 and response.content==b'bcd'
    assert response.headers['etag']=='"'+digest+'"'
    assert client.get(url.replace('signature=','signature=invalid')).status_code==403
    assert client.get(f'/download/jobs/{job["id"]}?expires=0&signature=bad').status_code==403


def test_worker_configuration_never_uses_ephemeral_vercel_disk(monkeypatch):
    monkeypatch.setenv('VERCEL','1');monkeypatch.setenv('ONDA_WORKER_MODE','true')
    assert jobs.local_store() is None
    with pytest.raises(ValueError):
        JobStore('/tmp/onda-jobs',b'x'*32)


def test_circuit_counts_exhausted_transient_failures_but_isolates_accounts():
    from api.operations import provider_circuit
    for _ in range(3):
        error=security.AudioError('Temporarily unavailable','upstream_unavailable',503)
        error.recovery_exhausted=True
        with pytest.raises(security.AudioError):
            with provider_circuit('owned-provider','account-a'):
                raise error
    with pytest.raises(security.AudioError):
        with provider_circuit('owned-provider','account-a'):
            pytest.fail('open circuit contacted the origin')
    with provider_circuit('owned-provider','account-b'):
        pass
