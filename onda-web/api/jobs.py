"""Optional persistent worker API. Never persist jobs on Vercel's ephemeral disk."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
import httpx
from .resources import ResourcePolicy
from .security import AudioError

TERMINAL = {'ready', 'failed', 'cancelled'}
_store = None
_store_lock = threading.Lock()


class JobStore:
    def __init__(self, directory, key):
        if not Path(directory).is_absolute():
            raise ValueError("absolute persistent path required")
        self.root = Path(directory).resolve()
        if not self.root.is_absolute() or Path('/tmp') == self.root or Path('/tmp') in self.root.parents:
            raise ValueError('persistent volume required')
        if len(key) != 32:
            raise ValueError('32 byte key required')
        self.key = key
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.database = self.root / 'jobs.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL, fingerprint TEXT NOT NULL, payload BLOB, checkpoint BLOB, status TEXT NOT NULL, priority INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL, lease REAL NOT NULL DEFAULT 0, fence TEXT, attempts INTEGER NOT NULL DEFAULT 0, progress TEXT NOT NULL DEFAULT \'{}\', file TEXT, UNIQUE(owner,idem))')
            db.execute('CREATE INDEX IF NOT EXISTS queue ON jobs(status,priority,created)')
            db.execute('CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL)')
        os.chmod(self.database, 0o600)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        return db

    def owner(self, identity):
        return hmac.new(self.key, ('owner:' + identity).encode(), hashlib.sha256).hexdigest()

    def seal(self, job, value):
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self.key).encrypt(nonce, json.dumps(value).encode(), job.encode())

    def open(self, job, value):
        return json.loads(AESGCM(self.key).decrypt(value[:12], value[12:], job.encode()))

    def create(self, owner, idem, payload, priority=1):
        fingerprint = hmac.new(self.key, json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM jobs WHERE owner=? AND idem=?', (owner, idem)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise AudioError('A identificação já pertence a outra tarefa.', 'idempotency_conflict', 409)
                return self.snapshot(previous), False
            pending = db.execute("SELECT count(*) FROM jobs WHERE owner=? AND status NOT IN ('ready','failed','cancelled')", (owner,)).fetchone()[0]
            if pending >= 4:
                raise AudioError('Sua fila está cheia. Aguarde uma tarefa terminar.', 'user_busy', 429)
            try:
                capacity = max(2,min(10000,int(os.environ.get('ONDA_MAX_PENDING_JOBS','1000'))))
            except ValueError:
                capacity = 1000
            total_pending = db.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('ready','failed','cancelled')").fetchone()[0]
            if total_pending >= capacity:
                raise AudioError('A fila do serviço está cheia. Tente novamente mais tarde.', 'busy', 429)
            payload_size = len(json.dumps(payload).encode())
            if shutil.disk_usage(self.root).free < ResourcePolicy.from_environment().reserve_bytes + payload_size*2 + 128*1024:
                raise AudioError('O armazenamento da fila está ocupado.', 'storage_busy', 503)
            job = secrets.token_hex(16)
            db.execute('INSERT INTO jobs(id,owner,idem,fingerprint,payload,status,priority,created,updated,expires) VALUES(?,?,?,?,?,?,?,?,?,?)', (job, owner, idem, fingerprint, self.seal(job, payload), 'queued', priority, now, now, now + 86400))
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            return self.snapshot(row), True

    def snapshot(self, row):
        result = {key: row[key] for key in ('id','status','attempts')}
        result.update(createdAt=row['created'], updatedAt=row['updated'], expiresAt=row['expires'], progress=json.loads(row['progress']))
        if row['file'] and row['status'] == 'ready':
            result['file'] = {k: v for k,v in json.loads(row['file']).items() if k != 'path'}
        return result

    def get(self, job, owner=None, *, include_expired=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if not row or owner is not None and not hmac.compare_digest(owner, row['owner']) or not include_expired and row['expires'] < time.time():
            raise AudioError('A tarefa não está disponível.', 'job_not_found', 404)
        return row

    def claim(self):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM jobs WHERE status='running' AND lease>?", (now,)).fetchone()[0] >= 2:
                return None
            # Crashed workers can resume retained input checkpoints. At most three claims.
            db.execute("UPDATE jobs SET status='queued', fence=NULL WHERE status='running' AND lease<? AND attempts<3", (now,))
            db.execute("UPDATE jobs SET status='failed', payload=NULL, checkpoint=NULL, progress=? WHERE status='running' AND lease<? AND attempts>=3", (json.dumps({'code':'worker_interrupted'}),now))
            row = db.execute("SELECT * FROM jobs j WHERE j.status='queued' AND j.expires>? AND NOT EXISTS(SELECT 1 FROM jobs a WHERE a.owner=j.owner AND a.status='running') ORDER BY (priority + min(2,(?-created)/300)) DESC,created LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            fence = secrets.token_hex(16)
            db.execute("UPDATE jobs SET status='running',fence=?,lease=?,updated=?,attempts=attempts+1 WHERE id=?", (fence, now+30, now,row['id']))
            return db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()

    def update(self, job, fence, *, progress=None, checkpoint=None, status=None, file=None):
        fields = {'updated':time.time(), 'lease':time.time()+30}
        if progress is not None:
            fields['progress'] = json.dumps(progress)
        if checkpoint is not None:
            fields['checkpoint'] = self.seal(job, checkpoint)
        if status:
            fields['status'] = status
        if file:
            fields['file'] = json.dumps(file)
        if status in TERMINAL:
            fields.update(payload=None,checkpoint=None)
        with self.connect() as db:
            changed = db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+" WHERE id=? AND fence=? AND status='running'", (*fields.values(),job,fence)).rowcount
        return bool(changed)

    def cancel(self, job, owner):
        self.get(job,owner)
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled',payload=NULL,checkpoint=NULL,updated=? WHERE id=? AND owner=? AND status NOT IN ('ready','failed','cancelled')", (time.time(),job,owner))
        return self.snapshot(self.get(job,owner))

    def heartbeat(self):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO heartbeat VALUES(1,?)', (time.time(),))

    def healthy(self):
        with self.connect() as db:
            row = db.execute('SELECT timestamp FROM heartbeat WHERE id=1').fetchone()
        return bool(row and time.time()-row[0]<30)

    def cleanup(self):
        import shutil
        with self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE expires<? AND (status!='running' OR lease<?)", (time.time(), time.time())).fetchall()
            for row in rows:
                shutil.rmtree(self.root / row['id'], ignore_errors=True)
                db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))


def local_store():
    global _store
    if os.environ.get('VERCEL') or os.environ.get('ONDA_WORKER_MODE','').lower() not in {'1','true','yes'}:
        return None
    with _store_lock:
        if _store is None:
            try:
                key = base64.urlsafe_b64decode(os.environ.get('ONDA_JOB_SECRET',''))
                directory = os.environ.get('ONDA_JOB_DIR','')
                if not Path(directory).is_absolute():
                    return None
                _store = JobStore(directory,key)
            except (ValueError, OSError):
                return None
        return _store


def gateway():
    value = os.environ.get('ONDA_WORKER_URL','').rstrip('/')
    parsed = urlsplit(value)
    if value and parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ('','/'):
        return value
    return None


def capabilities():
    store = local_store()
    policy = ResourcePolicy.from_environment()
    return {'configured': bool(store or gateway()), 'enabled': bool(store and store.healthy()),
            'healthy': bool(store and store.healthy()), 'mode': 'persistent' if store else 'gateway' if gateway() else 'disabled',
            'maxSourceMB': policy.max_source_bytes//1024**2 if store else None,
            'maxOutputMB': policy.max_output_bytes//1024**2 if store else None,
            'operationTimeoutSeconds': policy.process_seconds if store else None}


def register_jobs(app, model):
    def owner(request):
        principal = request.state.onda_auth
        if principal.kind != 'user' or not principal.user_id:
            raise AudioError('Esta operação exige uma conta de usuário.', 'auth_forbidden', 403)
        store = local_store()
        if not store:
            raise AudioError('O worker persistente ainda não está configurado.', 'worker_not_configured', 503)
        return store, store.owner(principal.user_id)

    async def proxy(request):
        # Fixed administrator-controlled origin, no redirects or arbitrary forwarding.
        base = gateway()
        if not base:
            return None
        if request.state.onda_auth.kind != 'user':
            raise AudioError('Esta operação exige uma conta.', 'auth_forbidden', 403)
        raw = await request.body()
        if len(raw)>128*1024:
            raise AudioError('Solicitação grande demais.', 'invalid_request', 413)
        headers = {k:request.headers[k] for k in ('authorization','content-type','origin','range','if-range') if k in request.headers}
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
            response = await client.request(request.method,base+request.url.path,headers=headers,content=raw)
        if response.status_code>=300 and response.status_code<400 or len(response.content)>256*1024:
            raise AudioError('O worker retornou uma resposta inválida.', 'worker_unavailable', 503)
        try:
            return JSONResponse(response.json(),status_code=response.status_code)
        except ValueError:
            raise AudioError('O worker está indisponível.', 'worker_unavailable',503) from None

    @app.get('/api/jobs/capabilities')
    async def job_capabilities(request: Request):
        forwarded = await proxy(request)
        return forwarded if forwarded is not None else await asyncio.to_thread(capabilities)

    @app.post('/api/jobs')
    async def create_job(request: Request):
        forwarded = await proxy(request)
        if forwarded is not None:
            return forwarded
        store, user = owner(request)
        if not store.healthy():
            raise AudioError('O worker está indisponível.', 'worker_unavailable',503)
        try:
            raw = await request.body()
            if len(raw)>128*1024:
                raise ValueError()
            data = json.loads(raw)
            if set(data)-{'download','idempotencyKey','priority'}:
                raise ValueError()
            body = model.model_validate(data['download'])
            key = data['idempotencyKey']
            priority = data.get('priority',1)
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',key) or type(priority) is not int or not 0<=priority<=2:
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise AudioError('Confira as opções da tarefa.', 'invalid_request',422) from None
        job, created = await asyncio.to_thread(store.create,user,key,body.model_dump(),priority)
        return {'job':job,'created':created}

    @app.get('/api/jobs')
    async def list_jobs(request: Request):
        forwarded = await proxy(request)
        if forwarded is not None:
            return forwarded
        store,user = owner(request)
        def read():
            with store.connect() as db:
                rows = db.execute('SELECT * FROM jobs WHERE owner=? AND expires>? ORDER BY created DESC LIMIT 30', (user,time.time())).fetchall()
            return {'jobs':[store.snapshot(row) for row in rows]}
        return await asyncio.to_thread(read)

    @app.get('/api/jobs/{job}')
    async def get_job(job: str, request: Request):
        forwarded = await proxy(request)
        if forwarded is not None:
            return forwarded
        store,user = owner(request)
        return {'job':store.snapshot(await asyncio.to_thread(store.get,job,user))}

    @app.delete('/api/jobs/{job}')
    async def cancel_job(job: str, request: Request):
        forwarded = await proxy(request)
        if forwarded is not None:
            return forwarded
        store,user = owner(request)
        return {'job':await asyncio.to_thread(store.cancel,job,user)}

    @app.post('/api/jobs/{job}/delivery')
    async def delivery(job: str, request: Request):
        forwarded = await proxy(request)
        if forwarded is not None:
            return forwarded
        store,user = owner(request)
        row = await asyncio.to_thread(store.get,job,user)
        if row['status']!='ready':
            raise AudioError('O arquivo ainda não está pronto.', 'job_not_ready',409)
        file = json.loads(row['file'])
        expiry = min(int(row['expires']),int(time.time())+1200)
        content = f'{job}:{expiry}:{user}:{file["sha256"]}'
        signature = hmac.new(store.key,content.encode(),hashlib.sha256).hexdigest()
        origin = os.environ.get('ONDA_WORKER_PUBLIC_URL','').rstrip('/')
        if not origin:
            origin = str(request.base_url).rstrip('/')
        return {'url':f'{origin}/download/jobs/{job}?expires={expiry}&signature={signature}', 'expiresAt':expiry,'file':store.snapshot(row)['file']}

    @app.api_route('/download/jobs/{job}',methods=['GET','HEAD'])
    async def signed_file(job: str, request: Request):
        store = local_store()
        if not store or not re.fullmatch(r'[a-f0-9]{32}',job):
            raise AudioError('O arquivo não está disponível.', 'job_not_found',404)
        row = await asyncio.to_thread(store.get,job)
        try:
            expiry = int(request.query_params['expires'])
            signature = request.query_params['signature']
            file = json.loads(row['file'])
            expected = hmac.new(store.key,f'{job}:{expiry}:{row["owner"]}:{file["sha256"]}'.encode(),hashlib.sha256).hexdigest()
            if row['status']!='ready' or expiry<time.time() or expiry>min(row['expires'],time.time()+1201) or not hmac.compare_digest(expected,signature):
                raise ValueError()
            path = (store.root/job/file['path']).resolve()
            if store.root/job not in path.parents or not path.is_file() or path.stat().st_size!=file['size'] or file.get('mtimeNs') is not None and path.stat().st_mtime_ns != file['mtimeNs']:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise AudioError('O link expirou ou não é válido.', 'auth_forbidden',403) from None
        # Starlette serves ranges asynchronously in bounded chunks, without buffering the file.
        return FileResponse(path,filename=file['filename'],media_type=file['mimeType'],headers={'Cache-Control':'private, no-store','ETag':'"'+file['sha256']+'"','Referrer-Policy':'no-referrer'})
