"""Durable server batches: real media pipeline, isolated private transport double."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, parse_qs
import zipfile
import io
import pytest
from fastapi.testclient import TestClient
from api import batch_store, batch_worker, batches, batch_subscriber, index, auth, engine, security
from api.account import BlobCredentials
from api.security import AudioError

URL='https://ondaittoux.online/canary.wav'
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def batch_environment(monkeypatch):
    files={};sent=[];lock=threading.RLock();counter=0
    def wire(method,url,*,headers,body=None,**kwargs):
        nonlocal counter
        parsed=urlsplit(url)
        with lock:
            if method=='POST':
                assert parsed.path=='/api/blob/delete'
                for path in json.loads(body)['urls']:files.pop(path,None)
                return 200,{},b'{}'
            path=parse_qs(parsed.query)['pathname'][0] if method=='PUT' else parsed.path.lstrip('/')
            if method=='GET':
                if path not in files:return 404,{},b''
                raw,tag=files[path];return 200,{'ETag':tag},raw
            assert headers['x-vercel-blob-access']=='private' and 'Bearer fixture_' in headers['Authorization']
            if path in files and headers.get('x-if-match')!=files[path][1]:return 412,{},b'{}'
            if hasattr(body,'read'):
                parts=[]
                while block:=body.read(4096):parts.append(block)
                body=b''.join(parts)
            counter+=1;tag='"fixture-'+str(counter)+'"';files[path]=(body,tag)
            return 201,{},json.dumps({'pathname':path,'etag':tag}).encode()
    class PrivateResponse:
        def __init__(self,url,headers):
            parsed=urlsplit(url);assert parsed.hostname=='fixturestore.private.blob.vercel-storage.com'
            path=parsed.path.lstrip('/');raw,tag=files.get(path,(b'',None))
            self.status_code=200 if tag else 404;self.headers={'ETag':tag};self.raw=raw
            if headers.get('If-Match') and headers['If-Match']!=tag:self.status_code=412
            if headers.get('Range') and tag:
                start,end=map(int,headers['Range'][6:].split('-'));self.status_code=206
                self.headers['Content-Range']=f'bytes {start}-{end}/{len(raw)}';self.raw=raw[start:end+1]
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,size):
            for offset in range(0,len(self.raw),size):yield self.raw[offset:offset+size]
    def get(url,*,headers,**kwargs):
        assert kwargs['allow_redirects'] is False and headers['Accept-Encoding']=='identity'
        return PrivateResponse(url,headers)
    async def send(owner,batch,step,**kwargs):sent.append((owner,batch,step))
    async def gc(*args):pass
    monkeypatch.setenv('ONDA_BATCH_ENABLED','true')
    monkeypatch.setattr(batch_store,'blob_http',wire)
    monkeypatch.setattr(batch_store.requests,'get',get)
    monkeypatch.setattr(BlobCredentials,'from_environment',classmethod(lambda cls:BlobCredentials('fixturestore','fixture_not_a_live_key')))
    monkeypatch.setattr(batches,'dispatch',send);monkeypatch.setattr(batch_worker,'dispatch',send)
    monkeypatch.setattr(batches,'schedule_cleanup',gc);monkeypatch.setattr(batch_worker,'schedule_cleanup',gc)
    from yt_dlp.networking import Response
    class OwnedTransport:
        def __init__(self,guard,**kwargs):self.guard=guard
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def send(self,request):
            name=Path(urlsplit(request.url).path).name
            assert name in {'canary.wav','canary.mp4'}
            path=ROOT/'public'/name
            response=Response(path.open('rb'),request.url,{'Content-Type':'audio/wav' if name.endswith('wav') else 'video/mp4','Content-Length':str(path.stat().st_size),'ETag':'"owned-fixture"'},200)
            return security.BoundedResponse(response,self.guard)
    monkeypatch.setattr(engine,'direct_handler',OwnedTransport)
    return files,sent

def submit(client,urls=None,key='a'*32,**options):
    return client.post('/api/batches',json={'urls':urls or [URL,URL+'?copy=2'],'settings':{'media_type':'audio','format':'mp3','quality':'128','trim_start':0.2,'trim_end':0.8},'idempotencyKey':key,**options})

def run_steps(sent):
    # Every step is a new invocation, after the enqueue request/client is gone.
    while sent:
        owner,batch,_=sent.pop(0)
        asyncio.run(batch_subscriber.consume({'owner':owner,'batch':batch}))

def test_batch_survives_closed_client_new_workers_and_delivers_real_zip(batch_environment):
    files,sent=batch_environment
    with TestClient(index.app) as client:
        response=submit(client);assert response.status_code==202
        job=response.json()['batch'];assert job['status']=='queued'
    run_steps(sent)
    with TestClient(index.app) as client:
        value=client.get('/api/batches/'+job['id']).json()['batch']
        assert value['status']=='ready' and all(i['status']=='ready' for i in value['items'])
        assert 'owner' not in json.dumps(value) and 'batches/v1/' not in json.dumps(value)
        response=client.get('/api/batches/'+job['id']+'/file',headers={'Range':f'bytes=0-{value["file"]["size"]-1}'})
        assert response.status_code==206 and hashlib.sha256(response.content).hexdigest()==value['file']['sha256']
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert len(set(archive.namelist()))==2 and archive.testzip() is None
            assert all(e.filename.endswith('.mp3') and e.file_size>1000 for e in archive.infolist())
        assert client.get('/api/batches/'+job['id']+'/file').status_code==416

def test_batches_idempotent_isolated_and_credentials_encrypted(batch_environment,monkeypatch):
    files,sent=batch_environment
    with TestClient(index.app) as client:
        first=submit(client,video_password='private-video-password').json()['batch']
        assert submit(client,video_password='private-video-password').json()['batch']['id']==first['id']
        assert submit(client,video_password='different').status_code==409
        encrypted=[raw for path,(raw,_) in files.items() if path.endswith('/request.bin')]
        assert encrypted and all(b'private-video-password' not in raw and URL.encode() not in raw for raw in encrypted)
        assert 'password' not in json.dumps(sent)
        async def other(*args):return auth.AuthPrincipal('user',user_id='other_user',session_id='other_session')
        monkeypatch.setattr(auth,'_verify_session',other)
        assert client.get('/api/batches/'+first['id']).status_code==404
        assert client.delete('/api/batches/'+first['id']).status_code==404
        assert client.get('/api/batches').json()=={'batches':[]}

def test_cancelled_and_expired_batches_are_cleaned_without_browser(batch_environment):
    files,sent=batch_environment
    with TestClient(index.app) as client:
        job=submit(client).json()['batch'];owner=batch_store.BatchStore.owner('user_fixture')
        assert client.delete('/api/batches/'+job['id']).status_code==200
    run_steps(sent)
    assert not any('/'+job['id']+'/' in path for path in files)

def test_temporary_failure_retries_step_without_redownloading_checkpoint(batch_environment,monkeypatch):
    files,sent=batch_environment
    with TestClient(index.app) as client:job=submit(client).json()['batch']
    owner,batch,_=sent.pop(0);asyncio.run(batch_worker.process(owner,batch))
    first=batch_store.state_of(batch_store.BatchStore(),owner,batch)['items'][0]['result']
    original=batch_worker.prepare_download;attempts=0
    def intermittent(*args,**kwargs):
        nonlocal attempts
        attempts+=1
        if attempts==1:raise AudioError('Temporary fixture','upstream_unavailable',503)
        return original(*args,**kwargs)
    monkeypatch.setattr(batch_worker,'prepare_download',intermittent)
    run_steps(sent)
    final=batch_store.state_of(batch_store.BatchStore(),owner,batch)
    assert final['status']=='ready' and final['items'][0]['result']==first and attempts==2

def test_permanent_auth_failure_is_not_retried_and_successes_still_pack(batch_environment,monkeypatch):
    files,sent=batch_environment
    with TestClient(index.app) as client:job=submit(client).json()['batch']
    original=batch_worker.prepare_download;calls=0
    def restricted(url,*args,**kwargs):
        nonlocal calls
        calls+=1
        if '?copy' in url:raise AudioError('Fixture login required','source_login_required',403)
        return original(url,*args,**kwargs)
    monkeypatch.setattr(batch_worker,'prepare_download',restricted)
    run_steps(sent)
    value=batch_store.state_of(batch_store.BatchStore(),batch_store.BatchStore.owner('user_fixture'),job['id'])
    assert value['status']=='ready' and calls==2 and value['items'][1]['code']=='source_login_required'
    with zipfile.ZipFile(io.BytesIO(files[value['file']['path']][0])) as archive:assert len(archive.namelist())==1

def test_admission_and_url_limits(batch_environment):
    files,sent=batch_environment
    with TestClient(index.app) as client:
        for number in range(4):assert submit(client,key=f'{number:032x}').status_code==202
        assert submit(client,key='f'*32).status_code==429
        assert submit(client,urls=['http://127.0.0.1/private']).status_code==400
        assert submit(client,urls=[URL+'?n='+str(n) for n in range(13)]).status_code==422

def test_missing_or_reclaimed_job_is_never_reprocessed(batch_environment):
    files,sent=batch_environment
    asyncio.run(batch_worker.process('a'*64,'b'*32))
    with TestClient(index.app) as client:job=submit(client).json()['batch']
    owner,batch,_=sent.pop(0);store=batch_store.BatchStore();value=batch_store.state_of(store,owner,batch)
    value.update(status='running',lease={'token':'worker-still-alive','until':time.time()+360})
    _,etag=store.read(batch_store.batch_path(owner,batch));store.write(batch_store.batch_path(owner,batch),value,etag)
    with pytest.raises(AudioError) as error:asyncio.run(batch_worker.process(owner,batch))
    assert error.value.code=='batch_busy'
    value['lease']['until']=0
    _,etag=store.read(batch_store.batch_path(owner,batch));store.write(batch_store.batch_path(owner,batch),value,etag)
    asyncio.run(batch_worker.process(owner,batch));run_steps(sent)
    assert batch_store.state_of(store,owner,batch)['status']=='ready'

def test_blob_range_change_and_encryption_tampering_rejected(batch_environment):
    files,sent=batch_environment
    with TestClient(index.app) as client:job=submit(client).json()['batch']
    owner=batch_store.BatchStore.owner('user_fixture');store=batch_store.BatchStore()
    path=batch_store.batch_path(owner,job['id'],'request.bin');raw,etag=files[path];files[path]=(raw[:-1]+bytes([raw[-1]^1]),etag)
    with pytest.raises(AudioError) as error:store.payload(owner,job['id'])
    assert error.value.code=='batch_request_unavailable'

def test_archive_names_and_resource_reservations(batch_environment):
    files,_=batch_environment;used=set()
    name=batch_worker.archive_name('../CON:evil/track','mp3',used)
    assert '/' not in name and '\\' not in name and ':' not in name
    assert batch_worker.archive_name('../CON:evil/track','mp3',used)!=name
    store=batch_store.BatchStore();expires=time.time()+100
    batch_worker.budget(store,'a',700*1024**2,expires,operations=10)
    batch_worker.budget(store,'a',700*1024**2,expires,operations=10)
    assert store.read(batch_worker.BUDGET_PATH)[0]['operations']==10
    with pytest.raises(AudioError) as error:batch_worker.budget(store,'b',100*1024**2,expires)
    assert error.value.code=='batch_storage_quota'

def test_retry_only_failed_items_uses_new_authorized_settings(batch_environment,monkeypatch):
    files,sent=batch_environment
    original=batch_worker.prepare_download
    def restricted(url,*args,**kwargs):
        if '?copy' in url:raise AudioError('Fixture restriction','source_login_required',403)
        return original(url,*args,**kwargs)
    monkeypatch.setattr(batch_worker,'prepare_download',restricted)
    with TestClient(index.app) as client:job=submit(client).json()['batch']
    run_steps(sent)
    monkeypatch.setattr(batch_worker,'prepare_download',original)
    with TestClient(index.app) as client:
        response=client.post('/api/batches/'+job['id']+'/retry',json={'settings':{'media_type':'audio','format':'m4a','quality':'128'},'idempotencyKey':'b'*32})
        assert response.status_code==202
        retried=response.json()['batch'];assert len(retried['items'])==1
    run_steps(sent)
    value=batch_store.state_of(batch_store.BatchStore(),batch_store.BatchStore.owner('user_fixture'),retried['id'])
    assert value['status']=='ready' and value['items'][0]['format']=='m4a'

def test_incomplete_creation_cannot_change_existing_idempotency_payload(batch_environment):
    files,sent=batch_environment;store=batch_store.BatchStore();owner=store.owner('user_fixture')
    body=batches.BatchInput(urls=[URL],idempotencyKey='d'*32)
    job=batches.create_batch(store,owner,body,index.DownloadInput)
    store.delete([batch_store.batch_path(owner,job['id'])])
    body.video_password='different-credentials'
    with pytest.raises(AudioError) as error:batches.create_batch(store,owner,body,index.DownloadInput)
    assert error.value.code=='batch_idempotency_conflict'

def test_restart_reclaims_unpublished_blob_before_new_upload(batch_environment):
    files,sent=batch_environment
    with TestClient(index.app) as client:job=submit(client).json()['batch']
    owner=batch_store.BatchStore.owner('user_fixture');store=batch_store.BatchStore()
    path=batch_store.batch_path(owner,job['id'],'media_orphan.bin')
    files[path]=(b'orphaned fixture upload','"orphan-etag"')
    def interrupted(row):row['artifacts'].append(path);return row
    store.mutate(batch_store.batch_path(owner,job['id']),interrupted)
    run_steps(sent)
    assert path not in files
    assert batch_store.state_of(store,owner,job['id'])['status']=='ready'
