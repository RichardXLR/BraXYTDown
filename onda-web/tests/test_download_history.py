"""History privacy, CAS races, bounded storage, failures and real download hooks."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit,parse_qs
import pytest
from fastapi.testclient import TestClient
from api import download_history as history,index,engine,security
from api.account import BlobCredentials
from api.jobs import JobStore

def record(identity='fixture',**changes):
    return {**history.Measurement(mode='inline',media_type='video',format='mp4',identity=identity).finish(status='ready',transferred=1024,output=512),**changes}

@pytest.fixture
def cloud(monkeypatch):
    files={}; calls=[]
    def transport(method,url,*,headers,body=None,**kwargs):
        assert headers['Authorization']=='Bearer fixture-not-a-real-key'
        parsed=urlsplit(url)
        if method=='GET':
            assert parsed.hostname=='historystore.private.blob.vercel-storage.com'
            path=parsed.path.removeprefix('/')
            if path not in files:return 404,{},b''
            value,etag=files[path];return 200,{'ETag':etag},value
        assert method=='PUT' and headers['x-vercel-blob-access']=='private'
        assert len(body)<=32768
        path=parse_qs(parsed.query)['pathname'][0]
        previous=files.get(path)
        if previous and headers.get('x-if-match')!=previous[1]:return 412,{},b''
        if not previous and headers.get('x-if-match'):return 412,{},b''
        files[path]=body,'"'+hashlib.sha256(body).hexdigest()+'"';calls.append(body)
        return 200,{},b'{}'
    monkeypatch.setattr(history,'blob_http',transport)
    return history.BlobHistory(BlobCredentials('historystore','fixture-not-a-real-key')),files,calls,transport

def test_history_is_account_isolated_and_deduplicated_without_credentials(cloud):
    store,files,calls,_=cloud
    store.append('user_A',record());store.append('user_A',record());store.append('user_B',record('other'))
    assert len(calls)==2 and len(files)==2
    assert [r['id'] for r in store.read('user_A')[0]]==['fixture']
    assert [r['id'] for r in store.read('user_B')[0]]==['other']
    assert all('user_' not in path and path.startswith('metrics/v1/') for path in files)
    with pytest.raises(ValueError):store.append('user_A',{**record(),'cookies':'secret'})
    with pytest.raises(ValueError):store.append('user_A',{**record(),'url':'https://private.example'})

def test_history_resolves_conditional_write_race_without_losing_records(cloud,monkeypatch):
    store,files,calls,transport=cloud
    store.append('user_A',record('first'))
    raced=False
    def race(method,url,**kwargs):
        nonlocal raced
        if method=='PUT' and not raced:
            raced=True
            monkeypatch.setattr(history,'blob_http',transport)
            store.append('user_A',record('concurrent'))
            monkeypatch.setattr(history,'blob_http',race)
        return transport(method,url,**kwargs)
    monkeypatch.setattr(history,'blob_http',race)
    store.append('user_A',record('second'))
    assert {r['id'] for r in store.read('user_A')[0]}=={'first','second','concurrent'}

def test_history_caps_bytes_count_and_removes_old_records_on_write(cloud):
    store,files,_,_=cloud
    store.append('user_A',record('expired',at=time.time()-31*86400))
    for n in range(140):store.append('user_A',record(str(n)))
    rows,_=store.read('user_A')
    assert len(rows)<=120 and 'expired' not in {r['id'] for r in rows}
    assert all(len(raw)<=32768 for raw,_ in files.values())

@pytest.mark.parametrize('changes',[{'processCpuSeconds':float('nan')},{'elapsedSeconds':-1},{'outputBytes':True},{'status':'fake_success'},{'code':'cookie=value'}])
def test_history_rejects_unmeasured_or_sensitive_fields(changes):
    with pytest.raises(ValueError):history.validate_record(record(**changes))

def test_failed_history_persistence_never_breaks_completed_download(monkeypatch):
    monkeypatch.setattr(history.BlobCredentials,'from_environment',classmethod(lambda cls:(_ for _ in ()).throw(RuntimeError('sensitive'))))
    history.persist('user_fixture',record())

def test_inline_success_and_failure_persist_real_outcome_after_cleanup(monkeypatch):
    measurements=[]
    monkeypatch.setattr(history,'persist',lambda user,row:measurements.append((user,row)))
    def media(url,directory,format,quality,guard,**kwargs):
        target=directory/'fixture.mp4';target.write_bytes(b'owned-fixture');guard.count(13)
        return target,{'title':'Own fixture'}
    monkeypatch.setattr(index,'prepare_download',media)
    client=TestClient(index.app)
    response=client.post('/api/download',json={'url':'https://example.org/own.mp4','media_type':'video','format':'mp4','quality':'source'},headers={'Accept':'application/x-onda-download;version=1'})
    assert response.status_code==200
    assert measurements[-1][0]=='user_fixture'
    assert measurements[-1][1]['status']=='ready'
    assert measurements[-1][1]['transferredBytes']==13 and measurements[-1][1]['outputBytes']==13
    def fail(*args,**kwargs):raise security.AudioError('Unavailable','upstream_unavailable',503)
    monkeypatch.setattr(index,'prepare_download',fail)
    response=client.post('/api/download',json={'url':'https://example.org/own.mp4','media_type':'video','format':'mp4','quality':'source'})
    assert response.status_code==503 and measurements[-1][1]['status']=='failed'
    assert measurements[-1][1]['code']=='upstream_unavailable'
    assert 'url' not in measurements[-1][1]

def test_worker_history_survives_queue_expiry_is_owned_and_idempotent():
    root=Path('artifacts/history-tests').resolve();root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as path:
        store=JobStore(path,b'x'*32)
        a,b=store.owner('user_a'),store.owner('user_b')
        store.record_measurement(a,record('worker-execution',mode='worker'))
        store.record_measurement(a,record('worker-execution',mode='worker'))
        store.cleanup()
        assert len(store.history(a))==1 and store.history(b)==[]
        restarted=JobStore(path,b'x'*32)
        assert restarted.history(a)[0]['id']=='worker-execution'

def test_history_endpoint_uses_verified_principal_not_client_identity(cloud,monkeypatch):
    store,_,_,_=cloud
    monkeypatch.setattr(history.BlobCredentials,'from_environment',classmethod(lambda cls:store.credentials))
    store.append('user_fixture',record('mine'));store.append('user_other',record('private'))
    response=TestClient(index.app).get('/api/downloads/history?user=user_other')
    assert response.status_code==200 and [r['id'] for r in response.json()['records']]==['mine']

@pytest.mark.parametrize('body',[b'[]',b'{}',b'{"schema":true,"records":[]}',b'{"schema":1,"records":"bad"}',b'{"schema":1,"schema":1,"records":[]}',b'{"schema":1,"records":[],"cookies":"sensitive"}'])
def test_invalid_stored_history_is_sanitized_without_overwriting_it(cloud,body):
    store,files,calls,_=cloud
    path=store.path('user_A');files[path]=body,'"etag"'
    with pytest.raises(security.AudioError) as error:store.read('user_A')
    assert error.value.code=='history_unavailable' and 'sensitive' not in error.value.message
    assert files[path][0]==body and not calls
