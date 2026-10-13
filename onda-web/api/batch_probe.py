"""Real queue execution probe, scoped exclusively to AutoCura machine identities."""
import asyncio
import os
import time
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from vercel.queue import send
from .batch_store import BatchStore, HEX32, probe_path
from .security import AudioError

TOPIC = 'onda-media-batches-v1'

def register_batch_probe(app, model):
    def scope(request):
        principal = request.state.onda_auth
        if principal.kind != 'machine' or not principal.machine_id:
            raise AudioError('Esta verificação exige a identidade de manutenção.', 'auth_forbidden', 403)
        return BatchStore.owner('probe:' + principal.machine_id), os.environ.get('VERCEL_DEPLOYMENT_ID', '')

    @app.post('/api/background/probe')
    async def submit_probe(body: model, request: Request):
        owner, deployment = scope(request)
        nonce = uuid.uuid4().hex
        path = probe_path(deployment, owner, nonce)
        store = BatchStore()
        await asyncio.to_thread(store.write, path, {'created': time.time(), 'expires': time.time() + 600, 'completed': False})
        try:
            await send(TOPIC, {'probe': nonce, 'owner': owner, 'deployment': deployment},
                       idempotency_key='probe-' + nonce, retention=600)
        except Exception:
            raise AudioError('A fila nativa não aceitou a verificação.', 'queue_unavailable', 503) from None
        return JSONResponse({'id': nonce, 'queued': True}, status_code=202, headers={'Cache-Control':'no-store'})

    @app.get('/api/background/probe')
    async def inspect_probe(request: Request):
        owner, deployment = scope(request)
        nonce = request.query_params.get('id', '')
        if not HEX32.fullmatch(nonce):
            raise AudioError('Verificação não encontrada.', 'not_found', 404)
        store = BatchStore(); path = probe_path(deployment, owner, nonce)
        value, _ = await asyncio.to_thread(store.read, path)
        if not value:
            raise AudioError('Verificação não encontrada.', 'not_found', 404)
        done = value.get('completed') is True
        if done:
            await asyncio.to_thread(store.delete, [path])
        return JSONResponse({'completed':done}, headers={'Cache-Control':'no-store'})
