"""Opt-in Web Push for completed batches (RFC 8291/8292).

Subscriptions are encrypted in private Blob, never exposed by a read API.
Push contains no media titles, links, cookies, tokens or delivery URLs. The
existing durable consumer retries notifications separately from media work.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import struct
import time
import uuid
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from fastapi.responses import JSONResponse

from .account import parse_json
from .batch_store import BatchStore, HEX64, batch_path, encryption_key, state_of
from .batches import require_user
from .security import AudioError

MAX_DEVICES = 8
SUBSCRIPTION_LIFETIME = 90 * 86400
P256_ORDER = int('ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551', 16)
PUSH_TRANSPORT = ContextVar('onda_push_transport', default=False)


class PushPrivacyFilter(logging.Filter):
    def filter(self, _record):
        return not PUSH_TRANSPORT.get()


# These clients otherwise log capability-bearing endpoint URLs at INFO/DEBUG.
# Suppress only push transport records, preserving unrelated HTTP diagnostics.
for _logger in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks'):
    logging.getLogger(_logger).addFilter(PushPrivacyFilter())


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def unb64(value, length):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('key')
    raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    if len(raw) != length or b64(raw) != value:
        raise ValueError('key')
    return raw


def signing_key():
    # Stable across workers/deployments, domain-separated from session encryption.
    # Rotating CLERK_SECRET_KEY requires registering this device again.
    seed = hmac.digest(encryption_key(), b'Onda Web Push VAPID signing v1', 'sha256')
    return ec.derive_private_key(int.from_bytes(seed, 'big') % (P256_ORDER - 1) + 1, ec.SECP256R1())


def public_key(key):
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def endpoint_url(value):
    if not isinstance(value, str) or not 20 <= len(value) <= 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError('endpoint')
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    # Only browser-owned push services. No arbitrary HTTP targets or redirects.
    trusted = host in {'fcm.googleapis.com', 'web.push.apple.com', 'updates.push.services.mozilla.com'}
    trusted = trusted or host.endswith('.push.services.mozilla.com') or host.endswith('.notify.windows.com')
    if (parsed.scheme != 'https' or not trusted or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or not parsed.path.startswith('/')):
        raise ValueError('endpoint')
    return value


def subscription_input(data):
    if not isinstance(data, dict) or set(data) - {'endpoint', 'keys', 'expirationTime'}:
        raise ValueError('subscription')
    endpoint = endpoint_url(data.get('endpoint'))
    keys = data.get('keys')
    if not isinstance(keys, dict) or set(keys) != {'p256dh', 'auth'}:
        raise ValueError('keys')
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(keys['p256dh'], 65))
    unb64(keys['auth'], 16)
    expires = data.get('expirationTime')
    if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time() * 1000):
        raise ValueError('expiration')
    return {'endpoint': endpoint, 'keys': keys, 'expires': min(time.time() + SUBSCRIPTION_LIFETIME, expires / 1000) if expires is not None else time.time() + SUBSCRIPTION_LIFETIME}


def device_id(endpoint):
    return hashlib.sha256(endpoint.encode()).hexdigest()


def subscriptions_path(owner):
    if not HEX64.fullmatch(owner or ''):
        raise ValueError('owner')
    return f'batches/v1/{owner}/push.json'


def binding_path(identity):
    if not HEX64.fullmatch(identity or ''):
        raise ValueError('device')
    return f'batches/v1/push-bindings/{identity}.json'


def decode_devices(document, path):
    if not document:
        return {}
    try:
        if document.get('version') != 1:
            raise ValueError('version')
        raw = base64.b64decode(document['cipher'], altchars=b'-_', validate=True)
        decoded = parse_json(AESGCM(encryption_key()).decrypt(raw[:12], raw[12:], path.encode()))
        if not isinstance(decoded, dict) or len(decoded) > MAX_DEVICES:
            raise ValueError('devices')
        return decoded
    except Exception:
        raise AudioError('Não foi possível ler os avisos deste dispositivo. Tente ativá-los novamente.', 'push_storage_invalid', 503) from None


def encode_devices(devices, path):
    nonce = os.urandom(12)
    raw = json.dumps(devices, separators=(',', ':'), allow_nan=False).encode()
    return {'version': 1, 'cipher': base64.urlsafe_b64encode(nonce + AESGCM(encryption_key()).encrypt(nonce, raw, path.encode())).decode()}


def update_devices(store, owner, transform, *, reset_invalid=False):
    path = subscriptions_path(owner)
    def update(document):
        try:
            devices = decode_devices(document, path)
        except AudioError:
            if not reset_invalid:
                raise
            devices = {}  # Explicit enrollment after encryption-key rotation.
        result = transform(devices)
        return encode_devices(result, path) if result != devices else None
    return store.mutate(path, update)


def save_subscription(store, owner, subscription):
    identity = device_id(subscription['endpoint'])
    proof = hashlib.sha256(b'Onda push device proof v1:' + json.dumps(subscription['keys'], sort_keys=True).encode()).hexdigest()
    def bind(prior):
        if prior and prior.get('expires', 0) > time.time() and prior.get('proof') and not hmac.compare_digest(prior['proof'], proof):
            raise AudioError('Reative os avisos neste navegador.', 'push_invalid_device', 422)
        return {'owner': owner, 'expires': subscription['expires'], 'proof': proof}
    prior, _ = store.read(binding_path(identity))
    bind(prior)  # Check proof before changing the encrypted device document.
    def add(devices):
        rows = {key: row for key, row in devices.items() if row['expires'] > time.time()}
        if identity not in rows and len(rows) >= MAX_DEVICES:
            raise AudioError('Há oito dispositivos com avisos ativos nesta conta. Desative um deles para adicionar outro.', 'push_device_limit', 429)
        rows[identity] = subscription
        return rows
    update_devices(store, owner, add, reset_invalid=True)
    store.mutate(binding_path(identity), bind)


def remove_subscription(store, owner, identity):
    # Revoke the binding first; another account's binding cannot be removed.
    store.mutate(binding_path(identity), lambda row: {'owner': None, 'expires': 0} if row and row.get('owner') == owner else None)
    update_devices(store, owner, lambda devices: {key: row for key, row in devices.items() if key != identity})


def encrypt_push(subscription, payload, *, key=None, salt=None):
    """Single-record aes128gcm, including the RFC 8188 binary header."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    ua = unb64(subscription['keys']['p256dh'], 65)
    sender = public_key(key)
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua))
    prk_key = hmac.digest(unb64(subscription['keys']['auth'], 16), shared, 'sha256')
    ikm = hmac.digest(prk_key, b'WebPush: info\x00' + ua + sender + b'\x01', 'sha256')
    prk = hmac.digest(salt, ikm, 'sha256')
    cek = hmac.digest(prk, b'Content-Encoding: aes128gcm\x00\x01', 'sha256')[:16]
    nonce = hmac.digest(prk, b'Content-Encoding: nonce\x00\x01', 'sha256')[:12]
    if len(payload) > 1000:
        raise ValueError('push_payload_too_large')
    return salt + struct.pack('!IB', 4096, len(sender)) + sender + AESGCM(cek).encrypt(nonce, payload + b'\x02', None)


async def send_push(client, subscription, payload, ttl, tag):
    endpoint = endpoint_url(subscription['endpoint'])
    parsed = urlsplit(endpoint)
    key = signing_key()
    token = jwt.encode({'aud': f'https://{parsed.netloc}', 'exp': int(time.time()) + 3600,
                        'sub': 'https://ondaittoux.online'}, key, algorithm='ES256')
    # Never consume or log response bodies (which can contain endpoint tokens).
    context = PUSH_TRANSPORT.set(True)
    try:
        async with client.stream('POST', endpoint, content=encrypt_push(subscription, payload), headers={
                'Authorization': f'vapid t={token}, k={b64(public_key(key))}',
                'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
                'TTL': str(ttl), 'Urgency': 'normal', 'Topic': tag}) as response:
            return response.status_code
    finally:
        PUSH_TRANSPORT.reset(context)


async def deliver_ready(owner, batch, generation):
    """Durable per-device receipts; retries never re-download or invalidate ZIPs."""
    store = BatchStore()
    try:
        value = await asyncio.to_thread(state_of, store, owner, batch)
    except AudioError as error:
        if error.status == 404:
            return
        raise
    if (value.get('generation') != generation or value['status'] != 'ready'
            or value['expires'] <= time.time() or value.get('notification', {}).get('complete')):
        return
    path = subscriptions_path(owner)
    document, _ = await asyncio.to_thread(store.read, path)
    devices = decode_devices(document, path)
    if not devices:
        return  # No additional state writes for accounts that did not opt in.
    token = uuid.uuid4().hex
    def claim(row):
        if (not row or row.get('generation') != generation or row['status'] != 'ready'
                or row['expires'] <= time.time() or row.get('notification', {}).get('complete')):
            return None
        notice = row.setdefault('notification', {'sent': []})
        if notice.get('until', 0) > time.time():
            raise AudioError('O aviso está sendo enviado.', 'push_busy', 503)
        notice.update({'token': token, 'until': time.time() + 180})
        return row
    value = await asyncio.to_thread(store.mutate, batch_path(owner, batch), claim)
    if not value or value.get('notification', {}).get('token') != token:
        return
    def receipt(identity=None, *, complete=False, outcome='accepted'):
        def record(row):
            if not row or row.get('generation') != generation or row.get('notification', {}).get('token') != token:
                return None
            notice = row['notification']
            if identity and identity not in notice['sent']:
                notice['sent'].append(identity)
                notice.setdefault('outcomes', {})[identity] = outcome
            if complete:
                notice['complete'] = True
            if complete or identity is None:
                notice['until'] = 0
                notice.pop('token', None)
            return row
        return store.mutate(batch_path(owner, batch), record)
    try:
        payload = json.dumps({'type': 'BATCH_READY', 'owner': owner, 'batch': batch,
                              'created': value['created'], 'expires': value['expires']}, separators=(',', ':')).encode()
        ttl = max(1, min(21600, int(value['expires'] - time.time())))
        tag = b64(hashlib.sha256(f'{owner}:{batch}:{generation}'.encode()).digest()[:24])
        transient = False
        # Bounded sequential sends: at most eight devices; private worker only.
        async with httpx.AsyncClient(timeout=httpx.Timeout(5), follow_redirects=False, trust_env=False) as client:
            for identity, subscription in devices.items():
                if identity in value['notification']['sent']:
                    continue
                binding, _ = await asyncio.to_thread(store.read, binding_path(identity))
                if subscription['expires'] <= time.time() or not binding or binding.get('owner') != owner or binding.get('expires', 0) <= time.time():
                    await asyncio.to_thread(receipt, identity, outcome='revoked')
                    continue
                current = await asyncio.to_thread(state_of, store, owner, batch)
                if current['status'] != 'ready' or current.get('generation') != generation or current['expires'] <= time.time():
                    return
                try:
                    status = await send_push(client, subscription, payload, ttl, tag)
                except (httpx.HTTPError, ValueError):
                    transient = True
                    continue
                if status in (404, 410):
                    await asyncio.to_thread(remove_subscription, store, owner, identity)
                if status in (408, 429) or status >= 500:
                    transient = True
                    continue
                await asyncio.to_thread(receipt, identity, outcome='accepted' if 200 <= status < 300 else 'expired' if status in (404, 410) else 'rejected')
        await asyncio.to_thread(receipt, complete=not transient)
        if transient:
            raise AudioError('O aviso será reenviado; o ZIP continua disponível.', 'push_temporarily_unavailable', 503)
    finally:
        await asyncio.to_thread(receipt)


async def request_data(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise AudioError('Revise a configuração de avisos.', 'push_invalid_subscription', 413)
    try:
        return parse_json(bytes(raw))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AudioError('Revise a configuração de avisos.', 'push_invalid_subscription', 422) from None


def register_notifications(app):
    @app.get('/api/batch-notifications')
    async def config(request: Request):
        owner = require_user(request)
        return JSONResponse({'publicKey': b64(public_key(signing_key())), 'owner': owner}, headers={'Cache-Control': 'no-store'})

    @app.post('/api/batch-notifications')
    async def subscribe(request: Request):
        owner = require_user(request)
        try:
            subscription = subscription_input(await request_data(request))
        except (ValueError, TypeError, KeyError):
            raise AudioError('Este navegador não forneceu um endereço de avisos válido.', 'push_invalid_subscription', 422) from None
        await asyncio.to_thread(save_subscription, BatchStore(), owner, subscription)
        return JSONResponse({'enabled': True}, headers={'Cache-Control': 'no-store'})

    @app.post('/api/batch-notifications/disable')
    async def unsubscribe(request: Request):
        owner = require_user(request)
        try:
            data = await request_data(request)
            if not isinstance(data, dict) or set(data) != {'endpoint'}:
                raise ValueError('endpoint')
            identity = device_id(endpoint_url(data['endpoint']))
        except (ValueError, TypeError):
            raise AudioError('Revise a configuração de avisos.', 'push_invalid_subscription', 422) from None
        await asyncio.to_thread(remove_subscription, BatchStore(), owner, identity)
        return JSONResponse({'enabled': False}, headers={'Cache-Control': 'no-store'})
