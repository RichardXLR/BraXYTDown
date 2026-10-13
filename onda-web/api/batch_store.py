"""Private, bounded, conditional Blob documents for durable batch work.

All paths are derived by the server. No storage credentials, object URLs or
provider responses are returned to clients or logged.
"""
from __future__ import annotations
import hashlib
import json
import re
import os
import hmac
import copy
from pathlib import Path
from urllib.parse import urlencode
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
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

    def mutate(self, path, transform):
        for _ in range(5):
            value, etag = self.read(path)
            updated = transform(copy.deepcopy(value))
            if updated is None or updated == value:
                return value
            try:
                self.write(path, updated, etag)
                return updated
            except Conflict:
                continue
        raise storage_error('batch_busy', 409)

    def put_file(self, path, source, *, mime='application/octet-stream', check=lambda: None):
        self.validate_path(path)
        source = Path(source); size = source.stat().st_size
        class CheckedFile:
            def __init__(self, stream): self.stream = stream
            def __len__(self): return size
            def tell(self): return self.stream.tell()
            def read(self, count=-1):
                check()
                return self.stream.read(min(1024*1024, count) if count and count > 0 else 1024*1024)
        with source.open('rb') as stream:
            status, _, raw = blob_http('PUT', BLOB_API + '?' + urlencode({'pathname':path}),
                headers={**self.headers(), 'x-vercel-blob-access':'private', 'x-add-random-suffix':'0',
                    'x-allow-overwrite':'0', 'x-content-type':mime, 'Content-Type':mime,
                    'Content-Length':str(size), 'x-cache-control-max-age':'60'},
                body=CheckedFile(stream), timeout=(5,30))
        if status not in (200,201): raise storage_error()
        try:
            result = json.loads(raw)
            if result.get('pathname') != path: raise ValueError('path')
            tag = result.get('etag')
            if not isinstance(tag,str) or not tag or len(tag)>256: raise ValueError('etag')
        except (ValueError,TypeError,AttributeError):
            raise storage_error('batch_upload_invalid') from None
        return {'path':path,'size':size,'etag':tag}

    def stream_file(self, artifact, *, start=0, end=None, check=lambda: None):
        path, size, tag = artifact['path'], artifact['size'], artifact['etag']
        end = size-1 if end is None else end
        if not (type(size) is int and 0 <= start <= end < size): raise storage_error('invalid_batch_range',416)
        check()
        headers={**self.headers(),'Accept-Encoding':'identity','Range':f'bytes={start}-{end}','If-Match':tag}
        try:
            with requests.get(self.url(path), headers=headers, stream=True, timeout=(5,20), allow_redirects=False) as response:
                if response.status_code not in (200,206): raise storage_error('batch_file_unavailable')
                if response.status_code == 200 and (start != 0 or end != size-1): raise storage_error('batch_range_unavailable')
                if response.status_code == 206 and response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}': raise storage_error('batch_file_changed')
                if response.headers.get('Content-Encoding','identity') != 'identity' or response.headers.get('ETag') != tag: raise storage_error('batch_file_changed')
                received=0
                for block in response.iter_content(64*1024):
                    check(); received+=len(block)
                    if received>end-start+1: raise storage_error('batch_file_changed')
                    yield block
                if received != end-start+1: raise storage_error('batch_file_incomplete')
        except requests.RequestException:
            raise storage_error('batch_file_unavailable') from None

    def put_payload(self, owner, batch, payload):
        path = batch_path(owner,batch,'request.bin')
        raw = json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>192*1024: raise storage_error('batch_request_too_large',413)
        nonce=os.urandom(12)
        encrypted=nonce+AESGCM(encryption_key()).encrypt(nonce,raw,path.encode())
        headers={**self.headers(),'x-vercel-blob-access':'private','x-add-random-suffix':'0','x-allow-overwrite':'0',
            'x-content-type':'application/octet-stream','Content-Type':'application/octet-stream','x-cache-control-max-age':'60'}
        status,_,_=blob_http('PUT',BLOB_API+'?'+urlencode({'pathname':path}),headers=headers,body=encrypted)
        if status not in (200,201,409,412): raise storage_error()
        return path

    def payload(self, owner, batch):
        path=batch_path(owner,batch,'request.bin')
        try:
            with requests.get(self.url(path),headers={**self.headers(),'Accept-Encoding':'identity'},stream=True,timeout=(5,10),allow_redirects=False) as response:
                if response.status_code != 200: raise storage_error('batch_request_unavailable')
                chunks=[];size=0
                for block in response.iter_content(4096):
                    size+=len(block)
                    if size>192*1024+64:raise storage_error('batch_state_invalid')
                    chunks.append(block)
            raw=b''.join(chunks)
            decoded=AESGCM(encryption_key()).decrypt(raw[:12],raw[12:],path.encode())
            return json.loads(decoded)
        except (requests.RequestException, ValueError, TypeError, InvalidTag, UnicodeError, RecursionError):
            raise storage_error('batch_request_unavailable') from None

def encryption_key():
    secret=os.environ.get('CLERK_SECRET_KEY','')
    if not secret.startswith(('sk_test_','sk_live_')):raise storage_error('batch_encryption_unavailable')
    return hmac.digest(secret.encode(),b'Onda private batch payload v1','sha256')

def batch_path(owner,batch,leaf='state.json'):
    if not HEX64.fullmatch(owner or '') or not HEX32.fullmatch(batch or '') or not re.fullmatch(r'[a-z0-9_-]+\.[a-z0-9]+',leaf):
        raise storage_error('invalid_batch_path',400)
    return f'batches/v1/{owner}/{batch}/{leaf}'

def state_of(store,owner,batch):
    value,_=store.read(batch_path(owner,batch))
    if not value:raise storage_error('batch_not_found',404)
    if value.get('schema')!=1 or value.get('owner')!=owner or value.get('id')!=batch:
        raise storage_error('batch_state_invalid')
    return value

def probe_path(deployment, owner, nonce):
    if not DEPLOYMENT.fullmatch(deployment or '') or not HEX64.fullmatch(owner or '') or not HEX32.fullmatch(nonce or ''):
        raise storage_error('invalid_batch_path', 400)
    return f'batch-probes/{deployment}/{owner}/{nonce}.json'
