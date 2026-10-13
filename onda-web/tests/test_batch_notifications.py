"""Web Push wire standard, private enrollment and durable completion/retries."""
import asyncio
import base64
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from api import auth, batch_notifications as push, batch_store, batch_subscriber, index
from api.security import AudioError
from test_batches import batch_environment, run_steps, submit


def subscription(number=1):
    return {'endpoint': f'https://fcm.googleapis.com/fcm/send/fixture-{number}', 'expirationTime': None,
            'keys': {'auth': 'BTBZMqHH6r4Tts7J_aSIgg', 'p256dh': 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4'}}


def ready_job(client, sent):
    job = submit(client).json()['batch']
    run_steps(sent)
    return client.get('/api/batches/' + job['id']).json()['batch']


def test_encryption_exactly_matches_published_rfc8291_example():
    key = ec.derive_private_key(int.from_bytes(push.unb64('yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw', 32), 'big'), ec.SECP256R1())
    raw = push.encrypt_push(subscription(), b'When I grow up, I want to be a watermelon',
                            key=key, salt=push.unb64('DGv6ra1nlYgDCS1FRnbzlw', 16))
    assert len(raw) == 144
    assert push.b64(raw) == ('DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg'
                             'Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVB'
                             'St2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN')


def test_authenticated_enrollment_is_encrypted_and_revocable(batch_environment):
    files, _ = batch_environment
    with TestClient(index.app) as client:
        public = client.get('/api/batch-notifications')
        assert public.status_code == 200 and public.headers['Cache-Control'] == 'no-store'
        assert set(public.json()) == {'publicKey', 'owner'} and len(push.unb64(public.json()['publicKey'], 65)) == 65
        assert client.post('/api/batch-notifications', json=subscription()).json() == {'enabled': True}
        raw = b''.join(data for data, _ in files.values())
        assert b'fcm.googleapis.com' not in raw and b'BTBZM' not in raw and b'fixture-1' not in raw
        assert client.post('/api/batch-notifications/disable', json={'endpoint': subscription()['endpoint']}).json() == {'enabled': False}
        path = push.subscriptions_path(public.json()['owner'])
        assert push.decode_devices(batch_store.BatchStore().read(path)[0], path) == {}
        assert client.get('/api/batch-notifications', headers={'Authorization': ''}).status_code == 401


@pytest.mark.parametrize('endpoint', ['http://fcm.googleapis.com/fcm/x', 'https://127.0.0.1/private',
    'https://fcm.googleapis.com.evil.test/token', 'https://evil.test/push', 'https://fcm.googleapis.com:444/x',
    'https://user:secret@fcm.googleapis.com/x', 'https://fcm.googleapis.com/x#private', 'https://fcm.googleapis.com/x\n'])
def test_subscription_cannot_turn_push_into_an_ssrf_proxy(batch_environment, endpoint):
    data = subscription(); data['endpoint'] = endpoint
    with TestClient(index.app) as client:
        assert client.post('/api/batch-notifications', json=data).status_code == 422


@pytest.mark.parametrize('mutation', [lambda data: data['keys'].update(auth='invalid'),
    lambda data: data['keys'].update(p256dh=push.b64(b'\x04' + b'\x00' * 64)),
    lambda data: data.update(expirationTime=1), lambda data: data.update(owner='someone_else')])
def test_rejects_invalid_keys_expiry_and_client_supplied_owner(batch_environment, mutation):
    data = subscription(); mutation(data)
    with TestClient(index.app) as client:
        assert client.post('/api/batch-notifications', json=data).status_code == 422


def test_real_wire_vapid_signature_and_expiry_without_redirects(caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger='httpx')
    calls = []
    def transport(request):
        calls.append(request)
        assert request.headers['Content-Encoding'] == 'aes128gcm'
        assert request.headers['TTL'] == '60' and request.headers['Topic'] == 'safe-tag'
        token = request.headers['Authorization'].split('t=', 1)[1].split(',', 1)[0]
        claims = jwt.decode(token, push.signing_key().public_key(), algorithms=['ES256'], audience='https://fcm.googleapis.com')
        assert claims['sub'] == 'https://ondaittoux.online' and 0 < claims['exp'] - time.time() <= 3600
        assert b'private-media-title' not in request.content
        return httpx.Response(302, headers={'Location': 'https://127.0.0.1/private'})
    async def send():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=False) as client:
            return await push.send_push(client, subscription(), b'private-media-title', 60, 'safe-tag')
    assert asyncio.run(send()) == 302 and len(calls) == 1
    assert 'fixture-1' not in caplog.text and 'vapid t=' not in caplog.text


def test_closed_client_completion_sends_once_without_private_media_data(batch_environment, monkeypatch):
    _, sent = batch_environment; calls = []
    async def send(_client, _subscription, payload, ttl, tag):
        calls.append((json.loads(payload), ttl, tag)); return 201
    monkeypatch.setattr(push, 'send_push', send)
    with TestClient(index.app) as client:
        assert client.post('/api/batch-notifications', json=subscription()).status_code == 200
        job = submit(client).json()['batch']
    # No page, request or client remains open while independent workers finish.
    run_steps(sent)
    owner = batch_store.BatchStore.owner('user_fixture')
    value = batch_store.state_of(batch_store.BatchStore(), owner, job['id'])
    assert value['status'] == 'ready' and len(calls) == 1
    assert set(calls[0][0]) == {'type', 'owner', 'batch', 'created', 'expires'}
    assert 0 < calls[0][1] <= 21600 and len(calls[0][2]) == 32
    asyncio.run(batch_subscriber.consume({'owner': owner, 'batch': job['id'], 'generation': value['generation']}))
    assert len(calls) == 1


def test_push_retry_does_not_process_media_again_or_duplicate_other_device(batch_environment, monkeypatch):
    _, sent = batch_environment
    with TestClient(index.app) as client:
        job = ready_job(client, sent)
        for number in (1, 2): assert client.post('/api/batch-notifications', json=subscription(number)).status_code == 200
    owner = batch_store.BatchStore.owner('user_fixture'); store = batch_store.BatchStore()
    state = batch_store.state_of(store, owner, job['id']); state.pop('notification', None); store.write(batch_store.batch_path(owner, job['id']), state, store.read(batch_store.batch_path(owner, job['id']))[1])
    calls = []
    async def send(_client, device, _payload, _ttl, _tag):
        calls.append(device['endpoint']); return 503 if device['endpoint'].endswith('-2') and len(calls) == 2 else 201
    monkeypatch.setattr(push, 'send_push', send)
    def no_media(*_args, **_kwargs): raise AssertionError('media work unexpectedly repeated')
    monkeypatch.setattr('api.batch_worker.prepare_download', no_media)
    generation = state['generation']
    with pytest.raises(AudioError, match='ZIP continua'):
        asyncio.run(batch_subscriber.consume({'owner': owner, 'batch': job['id'], 'generation': generation}))
    assert batch_store.state_of(store, owner, job['id'])['file'] == state['file']
    asyncio.run(batch_subscriber.consume({'owner': owner, 'batch': job['id'], 'generation': generation}))
    assert calls == [subscription(1)['endpoint'], subscription(2)['endpoint'], subscription(2)['endpoint']]


def test_account_switch_revokes_previous_binding_without_exposing_subscriptions(batch_environment, monkeypatch):
    _, sent = batch_environment
    with TestClient(index.app) as client:
        job = ready_job(client, sent)
        assert client.post('/api/batch-notifications', json=subscription()).status_code == 200
        async def other_user(*_args): return auth.AuthPrincipal('user', user_id='user_other', session_id='session_other')
        monkeypatch.setattr(auth, '_verify_session', other_user)
        assert client.post('/api/batch-notifications', json=subscription()).status_code == 200
        assert client.get('/api/batch-notifications').json()['owner'] == batch_store.BatchStore.owner('user_other')
    owner = batch_store.BatchStore.owner('user_fixture'); store = batch_store.BatchStore()
    state = batch_store.state_of(store, owner, job['id']); state.pop('notification', None); store.write(batch_store.batch_path(owner, job['id']), state, store.read(batch_store.batch_path(owner, job['id']))[1])
    async def unexpected(*_args): raise AssertionError('a notification crossed accounts')
    monkeypatch.setattr(push, 'send_push', unexpected)
    asyncio.run(push.deliver_ready(owner, job['id'], state['generation']))


def test_expired_provider_is_removed_and_device_limit_is_bounded(batch_environment, monkeypatch):
    _, sent = batch_environment
    with TestClient(index.app) as client:
        job = ready_job(client, sent)
        for number in range(8): assert client.post('/api/batch-notifications', json=subscription(number)).status_code == 200
        assert client.post('/api/batch-notifications', json=subscription(9)).status_code == 429
    owner = batch_store.BatchStore.owner('user_fixture'); store = batch_store.BatchStore()
    state = batch_store.state_of(store, owner, job['id']); state.pop('notification', None); store.write(batch_store.batch_path(owner, job['id']), state, store.read(batch_store.batch_path(owner, job['id']))[1])
    async def expired(*_args): return 410
    monkeypatch.setattr(push, 'send_push', expired)
    asyncio.run(push.deliver_ready(owner, job['id'], state['generation']))
    path = push.subscriptions_path(owner)
    assert push.decode_devices(store.read(path)[0], path) == {}


def test_explicit_enrollment_recovers_after_secret_rotation(batch_environment, monkeypatch):
    with TestClient(index.app) as client:
        first = client.get('/api/batch-notifications').json()['publicKey']
        assert client.post('/api/batch-notifications', json=subscription()).status_code == 200
        monkeypatch.setenv('CLERK_SECRET_KEY', 'sk_test_rotated_fixture_not_a_real_key')
        assert client.get('/api/batch-notifications').json()['publicKey'] != first
        assert client.post('/api/batch-notifications', json=subscription()).status_code == 200


def test_endpoint_alone_cannot_replace_a_devices_secret(batch_environment):
    with TestClient(index.app) as client:
        assert client.post('/api/batch-notifications', json=subscription()).status_code == 200
        modified = subscription(); modified['keys']['auth'] = push.b64(b'x' * 16)
        assert client.post('/api/batch-notifications', json=modified).status_code == 422
        owner = batch_store.BatchStore.owner('user_fixture'); path = push.subscriptions_path(owner)
        devices = push.decode_devices(batch_store.BatchStore().read(path)[0], path)
        assert devices[push.device_id(subscription()['endpoint'])]['keys']['auth'] == subscription()['keys']['auth']
