"""Browser-independent batches backed by private Blob and Vercel Queues."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import time
import os
import re
import math
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .account import parse_json
from .batch_store import BatchStore, Conflict, HEX32, HEX64, batch_path, encryption_key, state_of, storage_error
from .security import AudioError, public_url

TOPIC='onda-media-batches-v1'
GC_TOPIC='onda-batch-cleanup-v1'
MAX_ITEMS=12
MAX_ARCHIVE=350*1024**2
EXPIRY=6*3600
SAFE_SETTINGS=frozenset({'media_type','format','quality','video_resolution','trim_start','trim_end','strip_metadata','mute','normalize_audio'})

class BatchRetryInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    settings:dict=Field(default_factory=dict)
    cookies:str|None=Field(default=None,max_length=65536,repr=False)
    video_password:str|None=Field(default=None,max_length=256,repr=False)
    user_agent:str|None=Field(default=None,max_length=512,repr=False)
    idempotencyKey:str=Field(pattern=r'^[a-f0-9-]{32,36}$')

class BatchInput(BatchRetryInput):
    urls:list[str]=Field(min_length=1,max_length=MAX_ITEMS)

def enabled():
    return os.environ.get('ONDA_BATCH_ENABLED')=='true'

def capabilities():
    return {'configured':enabled(),'browserIndependent':True,'maxItems':MAX_ITEMS,
            'maxArchiveMB':MAX_ARCHIVE//1024**2,'expiresHours':EXPIRY//3600}

async def dispatch(owner,batch,step,*,delay=0,generation=None):
    from vercel.queue import send
    payload={'owner':owner,'batch':batch}
    if generation:payload['generation']=generation
    key=f'{owner}:{batch}:{generation}:{step}' if generation else f'{owner}:{batch}:{step}'
    await send(TOPIC,payload,idempotency_key=key,retention=86400,delay=delay)

async def schedule_cleanup(owner,batch,delay,*,generation=None):
    from vercel.queue import send
    payload={'owner':owner,'batch':batch}
    if generation:payload['generation']=generation
    key=f'gc:{owner}:{batch}:{generation}:{int(delay//60)}' if generation else f'gc:{owner}:{batch}:{int(delay//60)}'
    await send(GC_TOPIC,payload,idempotency_key=key,
               delay=math.ceil(max(0,delay))+2,retention=86400)

def public_state(value):
    result={key:value[key] for key in ('id','status','created','expires','cursor')}
    result['items']=[{k:item[k] for k in ('source','status','format','media_type','title','code','progress','size') if k in item} for item in value['items']]
    if value.get('code'):result['code']=value['code']
    if value.get('file'):
        result['file']={k:value['file'][k] for k in ('size','name','sha256')}
    return result

def index_path(owner):
    if not HEX64.fullmatch(owner or ''):raise storage_error('invalid_batch_path',400)
    return f'batches/v1/{owner}/index.json'

def initialize_batch(store,owner,body,model):
    if set(body.settings)-SAFE_SETTINGS:raise AudioError('Revise os ajustes do pacote.','invalid_batch_settings',422)
    urls=list(dict.fromkeys(body.urls))
    payload={'urls':urls,'settings':body.settings,'cookies':body.cookies,'video_password':body.video_password,'user_agent':body.user_agent}
    if len(json.dumps(payload).encode())>192*1024:raise AudioError('A lista ou sessão é muito grande.','batch_request_too_large',413)
    inputs=[]
    for url in urls:
        if not 8<=len(url)<=4096:raise AudioError('Informe links públicos completos.','unsafe_url',400)
        public_url(url,resolve=False)
        try:inputs.append(model.model_validate({**body.settings,'url':url,**{k:payload[k] for k in ('cookies','video_password','user_agent') if payload[k]}}))
        except ValidationError:raise AudioError('Revise os formatos, ajustes e dados de acesso.','invalid_batch_settings',422) from None
    batch=hashlib.sha256((owner+':'+body.idempotencyKey).encode()).hexdigest()[:32]
    fingerprint=hmac.digest(encryption_key(),json.dumps(payload,sort_keys=True,separators=(',',':')).encode(),'sha256').hex()
    old,_=store.read(batch_path(owner,batch))
    listing,_=store.read(index_path(owner))
    prior=next((row for row in (listing or {}).get('rows',[]) if row.get('id')==batch and row.get('expires',0)>time.time()),None)
    if prior and prior.get('fingerprint')!=fingerprint:
        raise AudioError('Esta solicitação já foi usada com outros ajustes.','batch_idempotency_conflict',409)
    if old:
        if old.get('fingerprint')!=fingerprint:raise AudioError('Esta solicitação já foi usada com outros ajustes.','batch_idempotency_conflict',409)
        if old['expires']<=time.time():raise AudioError('Este pacote expirou. Envie novamente com uma nova solicitação.','batch_expired',410)
        if old['status'] not in ('creating','creation_failed'):return old,None,None
    from .engine import source_for
    token=uuid.uuid4().hex
    def claim(current):
        if current:
            if current.get('fingerprint')!=fingerprint:raise AudioError('Solicitação duplicada com ajustes diferentes.','batch_idempotency_conflict',409)
            if current['expires']<=time.time():raise AudioError('Este pacote expirou.','batch_expired',410)
            if current['status'] not in ('creating','creation_failed'):return None
            if current.get('creationLease') and current['creationLease']['until']>time.time():
                raise AudioError('Esta solicitação está sendo preparada. Tente novamente em instantes.','batch_busy',503)
            current['status']='creating';current['creationLease']={'token':token,'until':time.time()+600}
            return current
        return {'schema':1,'id':batch,'owner':owner,'fingerprint':fingerprint,'status':'creating',
            'generation':uuid.uuid4().hex,'creationLease':{'token':token,'until':time.time()+600},
            'created':time.time(),'expires':time.time()+EXPIRY,'cursor':0,'dispatchVersion':0,'attempt':0,'lease':None,'artifacts':[],
            'items':[{'source':source_for(item.url)[:253],'status':'queued','format':item.format,
                      'media_type':item.media_type,'title':f'Arquivo {i+1}'} for i,item in enumerate(inputs)],'file':None}
    value=store.mutate(batch_path(owner,batch),claim)
    if value['status'] not in ('creating','creation_failed'):return value,None,None
    return value,payload,token

def reserve_creation(store,owner,value,token):
    batch=value['id'];fingerprint=value['fingerprint'];expires=value['expires']
    # Reserve a recent-list slot with CAS; only server-derived owner paths exist.
    def reserve(value):
        prior=next((r for r in (value or {}).get('rows',[]) if r.get('id')==batch and r.get('expires',0)>time.time()),None)
        if prior and prior.get('fingerprint')!=fingerprint:
            raise AudioError('Esta solicitação já foi usada com outros ajustes.','batch_idempotency_conflict',409)
        rows=[r for r in (value or {}).get('rows',[]) if r.get('expires',0)>time.time() and r.get('id')!=batch]
        active=0
        for row in rows:
            doc,_=store.read(batch_path(owner,row['id']))
            if (doc and (doc.get('status') in ('queued','running','packing') or
                    doc.get('status')=='creating' and (doc.get('creationLease') or {}).get('until',0)>time.time())) or (not doc and row.get('reservedUntil',0)>time.time()):active+=1
        if active>=4:raise AudioError('Há quatro pacotes em andamento. Aguarde ou cancele um deles.','batch_busy',429)
        rows.insert(0,{'id':batch,'expires':expires,'reservedUntil':time.time()+600,'fingerprint':fingerprint})
        return {'rows':rows[:12]}
    store.mutate(index_path(owner),reserve)
    from .batch_worker import reserve_submission
    reserve_submission(store,owner,batch,len(value['items']),value['expires'])

def finish_creation(store,owner,value,payload,token):
    batch=value['id']
    current=state_of(store,owner,batch)
    if current['expires']<=time.time() or current['status']!='creating' or (current.get('creationLease') or {}).get('token')!=token:
        raise AudioError('A solicitação foi cancelada ou reassumida.','batch_busy',409)
    store.put_payload(owner,batch,payload)
    def finish(row):
        if row['expires']<=time.time() or row['status']!='creating' or (row.get('creationLease') or {}).get('token')!=token:
            raise AudioError('A solicitação foi cancelada ou reassumida.','batch_busy',409)
        row['status']='queued';row.pop('creationLease',None);return row
    return store.mutate(batch_path(owner,batch),finish)

def undo_creation(store,owner,value,token):
    """Release admission without deleting a concurrent producer's payload."""
    batch=value['id']
    def failed(row):
        if not row or (row.get('creationLease') or {}).get('token')!=token:return None
        row['status']='creation_failed';row.pop('creationLease',None);return row
    current=state_of(store,owner,batch)
    if (current.get('creationLease') or {}).get('token')!=token:return
    from .batch_worker import budget
    budget(store,owner+':'+batch,0,value['expires'])
    def unreserve(row):
        if not row:return None
        row['rows']=[entry for entry in row.get('rows',[]) if entry['id']!=batch];return row
    store.mutate(index_path(owner),unreserve)
    # Keep the creation lease until compensation ends, so another producer
    # cannot lose its new reservation to this failed attempt.
    store.mutate(batch_path(owner,batch),failed)

async def create_batch(store,owner,body,model):
    value,payload,token=await asyncio.to_thread(initialize_batch,store,owner,body,model)
    if payload is None:return value
    try:
        # Persist a non-secret intent, then acknowledge durable GC BEFORE any
        # cookies are uploaded. Interrupted creation is visible to cleanup.
        await schedule_cleanup(owner,value['id'],value['expires']-time.time(),generation=value.get('generation'))
        await asyncio.to_thread(reserve_creation,store,owner,value,token)
        return await asyncio.to_thread(finish_creation,store,owner,value,payload,token)
    except Exception as error:
        try:
            # A PUT response may have been lost after the queued state committed.
            current=await asyncio.to_thread(state_of,store,owner,value['id'])
            if current['status']=='queued' and current.get('generation')==value.get('generation'):return current
            await asyncio.to_thread(undo_creation,store,owner,value,token)
        except Exception:pass # The acknowledged GC still reclaims interrupted work.
        if isinstance(error,AudioError):raise
        raise AudioError('Não foi possível preparar a fila. Tente novamente em instantes.','queue_unavailable',503) from None

def require_user(request):
    principal=request.state.onda_auth
    if principal.kind!='user' or not principal.user_id:raise AudioError('Entre na conta para usar sua fila.','auth_forbidden',403)
    if not enabled():raise AudioError('A fila do servidor está em preparação.','batch_not_configured',503)
    return BatchStore.owner(principal.user_id)

async def request_body(request, schema=BatchInput):
    size=0;chunks=[]
    async for chunk in request.stream():
        size+=len(chunk)
        if size>192*1024:raise AudioError('A lista ou sessão é muito grande.','batch_request_too_large',413)
        chunks.append(chunk)
    try:return schema.model_validate(parse_json(b''.join(chunks)))
    except (ValidationError,ValueError,TypeError,UnicodeError,RecursionError):
        raise AudioError('Revise os links e os ajustes do pacote.','invalid_batch_settings',422) from None

def register_batches(app,model):
    @app.get('/api/batches/capabilities')
    async def supported(request:Request):
        principal=request.state.onda_auth
        if principal.kind!='user':raise AudioError('Entre na conta.','auth_forbidden',403)
        return JSONResponse(capabilities(),headers={'Cache-Control':'no-store'})

    @app.post('/api/batches')
    async def enqueue(request:Request):
        owner=require_user(request);body=await request_body(request);store=BatchStore()
        value=await create_batch(store,owner,body,model)
        if value['status'] in ('queued','running','packing'):
            try:
                await dispatch(owner,value['id'],value['dispatchVersion'],generation=value.get('generation'))
            except Exception:raise AudioError('A fila não respondeu. Use a mesma lista para tentar novamente.','queue_unavailable',503) from None
        return JSONResponse({'batch':public_state(value)},status_code=202,headers={'Cache-Control':'no-store'})

    @app.get('/api/batches')
    async def recent(request:Request):
        owner=require_user(request);store=BatchStore()
        def read():
            value,_=store.read(index_path(owner));result=[]
            for row in (value or {}).get('rows',[]):
                if row.get('expires',0)>time.time() and HEX32.fullmatch(row.get('id','')):
                    doc,_=store.read(batch_path(owner,row['id']))
                    if doc and doc.get('owner')==owner and doc['status'] not in ('creating','creation_failed'):result.append(public_state(doc))
            return result
        return JSONResponse({'batches':await asyncio.to_thread(read)},headers={'Cache-Control':'no-store'})

    @app.get('/api/batches/{batch}')
    async def status(batch:str,request:Request):
        owner=require_user(request);store=BatchStore();value=await asyncio.to_thread(state_of,store,owner,batch)
        if value['expires']<time.time():raise AudioError('Este pacote expirou.','batch_expired',410)
        return JSONResponse({'batch':public_state(value)},headers={'Cache-Control':'no-store'})

    @app.post('/api/batches/{batch}/retry')
    async def retry(batch:str,request:Request):
        owner=require_user(request);store=BatchStore()
        value=await asyncio.to_thread(state_of,store,owner,batch)
        if value['expires']<time.time():raise AudioError('Este pacote expirou.','batch_expired',410)
        if value['status'] not in ('ready','failed'):raise AudioError('Aguarde o término deste pacote.','batch_busy',409)
        positions=[i for i,item in enumerate(value['items']) if item['status']=='failed']
        if not positions:raise AudioError('Não há arquivos pendentes para repetir.','batch_nothing_to_retry',409)
        body=await request_body(request,BatchRetryInput)
        original=await asyncio.to_thread(store.payload,owner,batch)
        next_body=BatchInput(**body.model_dump(),urls=[original['urls'][i] for i in positions])
        next_value=await create_batch(store,owner,next_body,model)
        try:
            await dispatch(owner,next_value['id'],next_value['dispatchVersion'],generation=next_value.get('generation'))
        except Exception:raise AudioError('Não foi possível enviar para a fila. Tente novamente.','queue_unavailable',503) from None
        return JSONResponse({'batch':public_state(next_value)},status_code=202,headers={'Cache-Control':'no-store'})

    @app.delete('/api/batches/{batch}')
    async def cancel(batch:str,request:Request):
        owner=require_user(request);store=BatchStore();existing=await asyncio.to_thread(state_of,store,owner,batch)
        def stop(value):
            if not value or value.get('generation')!=existing.get('generation'):
                raise AudioError('Este pacote expirou.','batch_expired',410)
            value['cleanupNotBefore']=max((value.get('lease') or {}).get('until',0),(value.get('creationLease') or {}).get('until',0))
            value['status']='cancelled';value['lease']=None
            for item in value['items']:
                if item['status'] in ('queued','running'):item['status']='cancelled'
            return value
        await asyncio.to_thread(store.mutate,batch_path(owner,batch),stop)
        try:await schedule_cleanup(owner,batch,600,generation=existing.get('generation'))
        except Exception:pass # The original expiry message still remains durable.
        return {'cancelled':True}

    @app.get('/api/batches/{batch}/file')
    async def file(batch:str,request:Request):
        owner=require_user(request);store=BatchStore();value=await asyncio.to_thread(state_of,store,owner,batch)
        if value['status']!='ready' or not value.get('file'):raise AudioError('O ZIP ainda não está disponível.','batch_not_ready',409)
        if value['expires']<time.time():raise AudioError('Este pacote expirou.','batch_expired',410)
        match=re.fullmatch(r'bytes=(\d+)-(\d+)',request.headers.get('Range',''))
        if not match:raise AudioError('Solicite um trecho do arquivo.','invalid_batch_range',416)
        start,end=map(int,match.groups());size=value['file']['size']
        if not 0<=start<=end<size or end-start+1>5*1024**2:raise AudioError('Trecho inválido.','invalid_batch_range',416)
        return StreamingResponse(store.stream_file(value['file'],start=start,end=end),status_code=206,
            media_type='application/zip',headers={'Content-Range':f'bytes {start}-{end}/{size}',
                'Content-Length':str(end-start+1),'Accept-Ranges':'bytes','Cache-Control':'no-store'})
