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

    @app.post('/api/background/batch-canary')
    async def submit_batch(body: model, request: Request):
        from .batches import BatchInput, create_batch, dispatch, schedule_cleanup, public_state
        from .index import DownloadInput
        owner, _ = scope(request)
        request_data = BatchInput(urls=[body.url, body.url + '?onda-batch-canary=2'],
            settings={'media_type':'audio','format':'mp3','quality':'128','trim_start':0.2,'trim_end':0.8},
            idempotencyKey=uuid.uuid4().hex)
        value = await asyncio.to_thread(create_batch, BatchStore(), owner, request_data, DownloadInput)
        await schedule_cleanup(owner, value['id'], value['expires']-time.time())
        await dispatch(owner, value['id'], value['dispatchVersion'])
        return JSONResponse({'batch':public_state(value)},status_code=202,headers={'Cache-Control':'no-store'})

    @app.get('/api/background/batch-canary')
    async def inspect_batch(request: Request):
        from .batch_store import state_of
        from .batches import public_state
        owner, _ = scope(request)
        store = BatchStore()
        value = await asyncio.to_thread(state_of,store,owner,request.query_params.get('id',''))
        if request.query_params.get('file')=='1':
            from fastapi.responses import StreamingResponse
            if value['status']!='ready' or not value.get('file') or value['file']['size']>1024*1024:
                raise AudioError('Pacote-canário indisponível.','batch_canary_invalid',409)
            return StreamingResponse(store.stream_file(value['file']),media_type='application/zip',headers={'Cache-Control':'no-store'})
        return JSONResponse({'batch':public_state(value)},headers={'Cache-Control':'no-store'})
