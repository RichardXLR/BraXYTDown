"""Private, bounded, conditional Blob documents for durable batch work.

All paths are derived by the server. No storage credentials, object URLs or
provider responses are returned to clients or logged.
"""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import urlencode
from .account import BlobCredentials, BLOB_API, BLOB_API_VERSION, blob_http
from .security import AudioError

HEX32 = re.compile(r'[a-f0-9]{32}\Z')
HEX64 = re.compile(r'[a-f0-9]{64}\Z')
DEPLOYMENT = re.compile(r'dpl_[A-Za-z0-9]{1,100}\Z')

def storage_error(code='batch_storage_unavailable', status=503):
    return AudioError('O armazenamento da fila está indisponível. Tente novamente.', code, status)

class Conflict(Exception):
    pass

class BatchStore:
    def __init__(self, credentials=None):
        self.credentials = credentials or BlobCredentials.from_environment()

    @staticmethod
    def owner(identity):
        return hashlib.sha256(('onda-batches-v1:' + identity).encode()).hexdigest()

    def headers(self):
        return {'Authorization': 'Bearer ' + self.credentials.token,
                'x-vercel-blob-store-id': self.credentials.store_id,
                'x-api-version': BLOB_API_VERSION}

    @staticmethod
    def validate_path(path):
        if (not isinstance(path, str) or len(path) > 400 or '..' in path
                or not re.fullmatch(r'(?:batch-probes|batches)/[A-Za-z0-9/_-]+\.[a-z0-9]+', path)):
            raise storage_error('invalid_batch_path', 400)

    def url(self, path):
        self.validate_path(path)
        return f'https://{self.credentials.store_id}.private.blob.vercel-storage.com/{path}'

    def read(self, path):
        status, headers, body = blob_http('GET', self.url(path) + '?cache=0', headers={
            **self.headers(), 'Accept-Encoding': 'identity'})
        if status == 404:
            return None, None
        if status != 200:
            raise storage_error('batch_storage_quota' if status in (402, 429) else 'batch_storage_unavailable')
        etag = next((v for k, v in headers.items() if k.lower() == 'etag'), '')
        if not etag or etag.startswith('W/') or len(etag) > 256 or any(ord(c) < 32 for c in etag):
            raise storage_error()
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError('document')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise storage_error('batch_state_invalid') from None
        return value, etag

    def write(self, path, value, etag=None):
        self.validate_path(path)
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        if len(body) > 32768:
            raise storage_error('batch_state_too_large', 413)
        headers = {**self.headers(), 'x-vercel-blob-access': 'private', 'x-add-random-suffix': '0',
                   'x-allow-overwrite': '1' if etag else '0', 'x-content-type': 'application/json',
                   'Content-Type': 'application/json', 'x-cache-control-max-age': '60'}
        if etag:
            headers['x-if-match'] = etag
        status, _, _ = blob_http('PUT', BLOB_API + '?' + urlencode({'pathname': path}), headers=headers, body=body)
        if status in (409, 412):
            raise Conflict()
        if status not in (200, 201):
            raise storage_error('batch_storage_quota' if status in (402, 429) else 'batch_storage_unavailable')

    def delete(self, paths):
        if not paths:
            return
        for path in paths:
            self.validate_path(path)
        status, _, _ = blob_http('POST', BLOB_API + '/delete', headers={
            **self.headers(), 'Content-Type': 'application/json'}, body=json.dumps({'urls': paths}).encode())
        if status not in (200, 204):
            raise storage_error()

def probe_path(deployment, owner, nonce):
    if not DEPLOYMENT.fullmatch(deployment or '') or not HEX64.fullmatch(owner or '') or not HEX32.fullmatch(nonce or ''):
        raise storage_error('invalid_batch_path', 400)
    return f'batch-probes/{deployment}/{owner}/{nonce}.json'
