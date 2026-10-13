"""One bounded media/ZIP step per invocation, durable checkpoints between steps."""
from __future__ import annotations
import asyncio
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import uuid
import zipfile
from .batch_store import BatchStore, batch_path, state_of, storage_error
from .batches import MAX_ARCHIVE, dispatch, schedule_cleanup
from .engine import prepare_download, source_for
from .progress import ProgressReporter
from .resources import ResourcePolicy, register_workspace, release_workspace
from .security import Guard, AudioError
from .recovery import retry_kind

BUDGET_PATH='batches/v1/global-budget.json'

def budget(store,key,amount,expires,*,operations=0):
    def change(value):
        value=value or {'month':time.strftime('%Y-%m',time.gmtime()),'operations':0,'allocations':{}}
        month=time.strftime('%Y-%m',time.gmtime())
        if value['month']!=month:value['month']=month;value['operations']=0
        rows={k:r for k,r in value['allocations'].items() if r['expires']>time.time()}
        charge=operations if key not in rows else 0
        if value['operations']+charge>1200:raise AudioError('A franquia reservada para pacotes deste mês foi atingida.','batch_monthly_limit',429)
        used=sum(r['bytes'] for k,r in rows.items() if k!=key)
        if used+amount>768*1024**2:raise AudioError('Salve e remova um pacote antigo para liberar espaço no servidor.','batch_storage_quota',429)
        if amount:rows[key]={'bytes':amount,'expires':expires}
        else:rows.pop(key,None)
        value['allocations']=rows;value['operations']+=charge
        return value
    store.mutate(BUDGET_PATH,change)

def reserve_submission(store,owner,batch,count,expires):
    key=owner+':'+batch
    # Conservative ceiling for the batch's writes; leave capacity for accounts.
    budget(store,key,1,expires,operations=30+count*24)

class BatchGuard(Guard):
    def __init__(self,store,owner,batch,token,**kwargs):
        super().__init__(**kwargs)
        self.store,self.owner,self.batch,self.token=store,owner,batch,token
        self.progress=ProgressReporter()
        self.partition=owner
        self.last_read=0
        self.read_lock=threading.RLock()
    def check(self):
        super().check()
        with self.read_lock:
            if time.monotonic()-self.last_read<5:return
            value=state_of(self.store,self.owner,self.batch)
            self.last_read=time.monotonic()
            if (value['status'] not in ('running','packing') or value['expires']<time.time()
                    or not value.get('lease') or value['lease']['token']!=self.token):
                self.abort();raise AudioError('O pacote foi cancelado ou reassumido.','cancelled',499)

def file_digest(path,check):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block:=stream.read(1024*1024):check();digest.update(block)
    return digest.hexdigest()

def archive_name(title,extension,used):
    import re,unicodedata
    value=unicodedata.normalize('NFC',str(title))
    value=re.sub(r'[\\/<>:"|?*\x00-\x1f\x7f]','-',value).strip(' .')
    value=value.encode()[:180].decode('utf-8',errors='ignore') or 'Onda'
    if value.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:value='Onda-'+value
    base=value;name=f'{base}.{extension}';suffix=2
    while name.casefold() in used:name=f'{base} ({suffix}).{extension}';suffix+=1
    used.add(name.casefold());return name

def update_owned(store,owner,batch,token,change):
    def update(value):
        if not value or value['status']=='cancelled' or not value.get('lease') or value['lease']['token']!=token:return None
        return change(value)
    return store.mutate(batch_path(owner,batch),update)

def track_artifact(store,owner,batch,token,path):
    def add(value):
        if path not in value['artifacts']:value['artifacts'].append(path)
        return value
    value=update_owned(store,owner,batch,token,add)
    if not value or not value.get('lease') or value['lease']['token']!=token:raise AudioError('Pacote cancelado.','cancelled',499)

def clear_orphans(store,value,token):
    """Reclaim unpublished uploads only after the previous lease has ended."""
    retained={item['result']['path'] for item in value['items'] if item.get('result')}
    if value.get('file'):retained.add(value['file']['path'])
    orphaned=[path for path in value['artifacts'] if path not in retained]
    if not orphaned:return
    prefix=batch_path(value['owner'],value['id']).rsplit('/',1)[0]+'/'
    if any(not path.startswith(prefix) for path in orphaned):raise storage_error('invalid_batch_path',400)
    store.delete(orphaned)
    def remove(row):row['artifacts']=[path for path in row['artifacts'] if path not in orphaned];return row
    update_owned(store,value['owner'],value['id'],token,remove)
    published=sum(item.get('result',{}).get('size',0) for item in value['items'])
    budget(store,value['owner']+':'+value['id'],published or 1,value['expires'])

def pack(store,value,guard,directory):
    ready=[item for item in value['items'] if item.get('result')]
    if not ready:raise AudioError('Nenhum arquivo foi concluído para este pacote.','batch_empty',422)
    total=sum(item['result']['size'] for item in ready)
    if total>MAX_ARCHIVE:raise AudioError('O pacote excede a capacidade da hospedagem atual.','batch_too_large',413)
    target=directory/'Onda.zip';used=set()
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
        for item in ready:
            guard.check()
            data=item['result'];name=archive_name(item['title'],item['format'],used)
            digest=hashlib.sha256();received=0
            with archive.open(name,'w',force_zip64=data['size']>=2**31) as entry:
                for block in store.stream_file(data,check=guard.check):
                    entry.write(block);received+=len(block);digest.update(block);guard.check_workspace()
            if received!=data['size'] or digest.hexdigest()!=data['sha256']:raise AudioError('Um arquivo mudou durante o empacotamento.','batch_integrity_failed',502)
    guard.check_output(target.stat().st_size)
    if target.stat().st_size>MAX_ARCHIVE:raise AudioError('O ZIP excede a capacidade do pacote.','batch_too_large',413)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:raise AudioError('O pacote não passou na verificação.','batch_integrity_failed',502)
    return target

async def process(owner,batch,generation=None):
    store=BatchStore()
    try:value=await asyncio.to_thread(state_of,store,owner,batch)
    except AudioError as error:
        if error.status==404:return
        raise
    if value.get('generation')!=generation:return
    if value['status'] in ('creating','creation_failed'):return
    if value['expires']<time.time() or value['status']=='cancelled':
        await cleanup(owner,batch,generation);return
    if value['status'] in ('ready','failed'):
        await schedule_cleanup(owner,batch,value['expires']-time.time(),generation=value.get('generation'));return
    token=uuid.uuid4().hex
    def claim(row):
        if row.get('generation')!=generation or row['status'] in ('ready','failed','cancelled','creating','creation_failed') or row['expires']<time.time():return None
        if row.get('lease') and row['lease']['until']>time.time():raise AudioError('Outro worker está processando esta etapa.','batch_busy',503)
        row['status']='running' if row['cursor']<len(row['items']) else 'packing'
        row['lease']={'token':token,'until':time.time()+360}
        row['attempt']=row.get('attempt',0)+1
        if row['cursor']<len(row['items']):row['items'][row['cursor']]['status']='running'
        return row
    value=await asyncio.to_thread(store.mutate,batch_path(owner,batch),claim)
    if not value or value['status'] not in ('running','packing') or value.get('lease',{}).get('token')!=token:return
    index=value['cursor'];policy=ResourcePolicy.from_environment();reporter=None
    with tempfile.TemporaryDirectory(prefix='onda-batch-') as temp:
        directory=Path(temp);register_workspace(directory)
        guard=BatchGuard(store,owner,batch,token,seconds=230,maximum_bytes=policy.max_transfer_bytes,
                         resource_policy=policy,workspace=directory)
        reporter=guard.progress;task=None
        try:
            await asyncio.to_thread(clear_orphans,store,value,token)
            if index<len(value['items']):
                from .index import DownloadInput
                payload=await asyncio.to_thread(store.payload,owner,batch)
                url=payload['urls'][index]
                data=DownloadInput.model_validate({**payload['settings'],'url':url,
                    **{k:payload[k] for k in ('cookies','video_password','user_agent') if payload.get(k)}})
                cookies=data.cookies
                if cookies:
                    from .cookies import parse_netscape
                    try:
                        jar=parse_netscape(cookies,url,direct_media=source_for(url)=='Arquivo direto');jar.clear()
                    except AudioError as error:
                        if error.code=='cookie_domain':cookies=None
                        else:raise
                task=asyncio.create_task(asyncio.to_thread(prepare_download,url,directory,data.format,data.quality,guard,
                    cookies=cookies,settings=data.settings(),session=data.session_options()))
                last_write=0;last_stage=None
                while not task.done():
                    await asyncio.wait({task},timeout=1)
                    latest=None
                    import queue
                    while True:
                        try:latest=reporter.events.get_nowait()
                        except queue.Empty:break
                    if latest and (latest['stage']!=last_stage or time.monotonic()-last_write>=45):
                        def progress(row):row['items'][index]['progress']=latest;return row
                        await asyncio.to_thread(update_owned,store,owner,batch,token,progress)
                        last_write=time.monotonic();last_stage=latest['stage']
                target,details=await task;guard.check()
                existing=sum(item.get('result',{}).get('size',0) for item in value['items'])
                size=target.stat().st_size
                if size+existing>MAX_ARCHIVE:raise AudioError('O conjunto ultrapassa a capacidade do pacote.','batch_too_large',413)
                await asyncio.to_thread(budget,store,owner+':'+batch,existing+size,value['expires'])
                path=batch_path(owner,batch,'media_'+token+'.bin')
                await asyncio.to_thread(track_artifact,store,owner,batch,token,path)
                artifact=await asyncio.to_thread(store.put_file,path,target,check=guard.check)
                artifact['sha256']=await asyncio.to_thread(file_digest,target,guard.check)
                def finish(row):
                    row['items'][index].update({'status':'ready','title':str(details.get('title') or f'Arquivo {index+1}')[:160],
                        'size':size,'result':artifact,'progress':{'stage':'ready','downloadedBytes':size,'totalBytes':size}})
                    row['cursor']=index+1;row['status']='queued';row['lease']=None;row['attempt']=0;row['dispatchVersion']+=1;return row
                value=await asyncio.to_thread(update_owned,store,owner,batch,token,finish)
            else:
                target=await asyncio.to_thread(pack,store,value,guard,directory);guard.check()
                source_size=sum(item.get('result',{}).get('size',0) for item in value['items'])
                await asyncio.to_thread(budget,store,owner+':'+batch,source_size+target.stat().st_size,value['expires'])
                path=batch_path(owner,batch,'zip_'+token+'.zip')
                await asyncio.to_thread(track_artifact,store,owner,batch,token,path)
                artifact=await asyncio.to_thread(store.put_file,path,target,mime='application/zip',check=guard.check)
                artifact.update({'sha256':await asyncio.to_thread(file_digest,target,guard.check),'name':'Onda-'+batch[:8]+'.zip'})
                def finish(row):row['file']=artifact;row['status']='ready';row['lease']=None;return row
                value=await asyncio.to_thread(update_owned,store,owner,batch,token,finish)
                if value and value.get('file') and value['file']['path']==path:
                    sources=[item['result']['path'] for item in value['items'] if item.get('result')]
                    await asyncio.to_thread(store.delete,sources)
                    await asyncio.to_thread(budget,store,owner+':'+batch,artifact['size'],value['expires'])
            if value and value['status']=='queued':await dispatch(owner,batch,value['dispatchVersion'],generation=value.get('generation'))
            elif value:await schedule_cleanup(owner,batch,value['expires']-time.time(),generation=value.get('generation'))
        except Exception as error:
            code=error.code if isinstance(error,AudioError) else 'batch_processing_failed'
            def failed(row):
                if (retry_kind(error)=='transient' or code in {'batch_storage_unavailable','batch_file_unavailable','batch_file_incomplete','batch_upload_invalid'}) and row.get('attempt',0)<3:
                    row['status']='queued';row['lease']=None;row['dispatchVersion']+=1
                    if index<len(row['items']):row['items'][index]['progress']={'stage':'recoverable_failure','code':code}
                    return row
                if index<len(row['items']):
                    row['items'][index].update({'status':'failed','code':code});row['cursor']=index+1;row['status']='queued'
                else:row['status']='failed';row['code']=code
                row['lease']=None;row['attempt']=0;row['dispatchVersion']+=1;return row
            value=await asyncio.to_thread(update_owned,store,owner,batch,token,failed)
            if value and value['status']=='queued':await dispatch(owner,batch,value['dispatchVersion'],delay=min(60,5*2**value.get('attempt',0)) if value.get('attempt') else 0,generation=value.get('generation'))
            elif value and value['status']!='cancelled':await schedule_cleanup(owner,batch,value['expires']-time.time(),generation=value.get('generation'))
        finally:
            guard.abort()
            if task is not None:
                try:await asyncio.shield(task)
                except Exception:pass
            release_workspace(directory)

async def cleanup(owner,batch,generation=None):
    store=BatchStore()
    try:value=await asyncio.to_thread(state_of,store,owner,batch)
    except AudioError as error:
        if error.status==404:
            # Legacy partial creations may have a payload without a state.
            # A fenced generation must never delete a later creation.
            if generation is None:
                # Claim the vacant intent conditionally. A creation that wins
                # this race must keep both its payload and resource budget.
                from .batch_store import Conflict
                try:
                    await asyncio.to_thread(store.write,batch_path(owner,batch),{
                        'schema':1,'id':batch,'owner':owner,'generation':None,'status':'cancelled',
                        'fingerprint':'cleanup','created':time.time(),'expires':time.time()-1,'cursor':0,'items':[],
                        'artifacts':[],'file':None})
                except Conflict:return
                await asyncio.to_thread(store.delete,[batch_path(owner,batch,'request.bin')])
                await asyncio.to_thread(budget,store,owner+':'+batch,0,time.time())
                await asyncio.to_thread(store.delete,[batch_path(owner,batch)])
            return
        raise
    if value.get('generation')!=generation:return
    quiet=max((value.get('lease') or {}).get('until',0),(value.get('creationLease') or {}).get('until',0),value.get('cleanupNotBefore',0))
    if quiet>time.time():
        # A timed-out PUT can finish after its client loses the lease. Never
        # remove the intent while that invocation could still create a blob.
        await schedule_cleanup(owner,batch,quiet-time.time(),generation=generation);return
    if value['status']!='cancelled' and value['expires']>time.time():
        await schedule_cleanup(owner,batch,value['expires']-time.time(),generation=value.get('generation'));return
    paths=[batch_path(owner,batch,'request.bin'),*value['artifacts']]
    prefix=batch_path(owner,batch).rsplit('/',1)[0]+'/'
    if any(not path.startswith(prefix) for path in paths):raise storage_error('invalid_batch_path',400)
    await asyncio.to_thread(store.delete,list(dict.fromkeys(paths)))
    await asyncio.to_thread(budget,store,owner+':'+batch,0,value['expires'])
    # Delete the intent last: its generation fences late messages and prevents
    # a new submission from racing deletion of this generation's payload.
    await asyncio.to_thread(store.delete,[batch_path(owner,batch)])
