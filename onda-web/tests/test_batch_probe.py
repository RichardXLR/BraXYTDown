"""Queue subscriber and durable private store; no real storage or billing calls."""
import asyncio
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import pytest
from api import batch_probe, batch_store, batch_subscriber, auth, index
from api.account import BlobCredentials
from api.security import AudioError

@pytest.fixture
def probe(monkeypatch):
    blobs = {}; sent = []; counter = 0
    def wire(method, url, *, headers, body=None, **kwargs):
        nonlocal counter
        from urllib.parse import urlsplit, parse_qs
        parsed = urlsplit(url)
        if method == 'POST':
            assert parsed.path == '/api/blob/delete'
            for path in json.loads(body)['urls']: blobs.pop(path, None)
            return 200, {}, b'{}'
        path = parse_qs(parsed.query)['pathname'][0] if method == 'PUT' else parsed.path.lstrip('/')
        if method == 'GET':
            if path not in blobs: return 404, {}, b''
            value, tag = blobs[path]
            return 200, {'ETag': tag}, value
        assert headers['x-vercel-blob-access'] == 'private'
        if path in blobs and headers.get('x-if-match') != blobs[path][1]: return 412, {}, b'{}'
        counter += 1; blobs[path] = body, '"etag-' + str(counter) + '"'
        return 201, {}, b'{}'
    async def send(topic, payload, **kwargs): sent.append((topic, payload, kwargs)); return 'opaque-fixture-message'
    monkeypatch.setattr(batch_store, 'blob_http', wire)
    monkeypatch.setattr(batch_probe, 'send', send)
    monkeypatch.setattr(BlobCredentials, 'from_environment', classmethod(lambda cls: BlobCredentials('fixturestore', 'fixture_not_a_live_credential')))
    monkeypatch.setenv('VERCEL_DEPLOYMENT_ID', 'dpl_probeFixture')
    app = FastAPI()
    @app.exception_handler(AudioError)
    async def fixture_error(request, error):
        return JSONResponse({'code': error.code}, status_code=error.status)
    @app.middleware('http')
    async def fixture_identity(request, call_next):
        request.state.onda_auth = auth.AuthPrincipal('machine', machine_id=request.headers.get('X-Fixture-Machine', 'fixture_machine_a'))
        return await call_next(request)
    batch_probe.register_batch_probe(app, index.LinkInput)
    with TestClient(app) as client:
        yield client, blobs, sent

def test_native_subscriber_survives_original_request_and_store_recreation(probe):
    client, blobs, sent = probe
    response = client.post('/api/background/probe', json={'url':'https://onda-audio.vercel.app/canary.wav'})
    assert response.status_code == 202
    nonce = response.json()['id']
    assert client.get('/api/background/probe?id=' + nonce).json() == {'completed':False}
    assert len(sent) == 1 and set(sent[0][1]) == {'probe', 'owner', 'deployment'}
    assert 'cookies' not in json.dumps(sent) and 'Bearer' not in json.dumps(sent)
    # A separate store and callback run execute after enqueue's HTTP response.
    asyncio.run(batch_subscriber.consume(sent[0][1]))
    assert batch_store.BatchStore().read(next(iter(blobs)))[0]['completed'] is True
    assert client.get('/api/background/probe?id=' + nonce).json() == {'completed':True}
    assert not blobs

def test_probe_is_isolated_between_machine_identities(probe):
    client, blobs, sent = probe
    nonce = client.post('/api/background/probe', json={'url':'https://onda-audio.vercel.app/canary.wav'}).json()['id']
    response = client.get('/api/background/probe?id=' + nonce, headers={'X-Fixture-Machine':'fixture_machine_b'})
    assert response.status_code == 404 and blobs
    assert client.get('/api/background/probe?id=' + nonce).json()['completed'] is False

def test_expired_or_wrong_deployment_probe_is_never_completed(probe):
    client, blobs, sent = probe
    client.post('/api/background/probe', json={'url':'https://onda-audio.vercel.app/canary.wav'})
    asyncio.run(batch_subscriber.consume({**sent[0][1], 'deployment':'dpl_otherFixture'}))
    path = next(iter(blobs)); data, tag = blobs[path]; value = json.loads(data)
    assert value['completed'] is False
    value['expires'] = 0; blobs[path] = json.dumps(value).encode(), tag
    asyncio.run(batch_subscriber.consume(sent[0][1]))
    assert json.loads(blobs[path][0])['completed'] is False

def test_probe_store_rejects_traversal_and_cross_namespace_paths(probe):
    store = batch_store.BatchStore()
    for path in ('accounts/v1/owner.json', 'batch-probes/../account.json', 'https://foreign.invalid/file.json'):
        with pytest.raises(Exception) as error: store.read(path)
        assert error.value.code == 'invalid_batch_path'

def test_probe_public_endpoint_rejects_anonymous_and_user_principals():
    with TestClient(index.app) as client:
        assert client.get('/api/background/probe?id=' + 'a'*32, headers={'Authorization':''}).status_code == 401
        assert client.post('/api/background/probe', json={'url':'https://onda-audio.vercel.app/canary.wav'}).status_code == 403
