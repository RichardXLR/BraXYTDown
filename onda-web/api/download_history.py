"""Bounded, account-isolated measurements. Never store media URLs or sessions."""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import resource
import secrets
import time
import urllib.parse
from fastapi import Request
from .account import BlobCredentials,BLOB_API,BLOB_API_VERSION,blob_http,user_identity,parse_json
from .security import AudioError

RETENTION_SECONDS=30*86400
MAX_RECORDS=120


class Measurement:
    def __init__(self, *, mode, media_type, format, identity=None):
        self.started=time.monotonic()
        self.created=time.time()
        self.before=self.usage()
        self.record={'id':identity or secrets.token_hex(16),'at':self.created,'mode':mode,'mediaType':media_type,'format':format}

    @staticmethod
    def usage():
        own=resource.getrusage(resource.RUSAGE_SELF)
        children=resource.getrusage(resource.RUSAGE_CHILDREN)
        return own.ru_utime+own.ru_stime+children.ru_utime+children.ru_stime,max(own.ru_maxrss,children.ru_maxrss)*1024

    def finish(self, *, status, transferred=0, output=0, code=None, attempt=1):
        cpu,rss=self.usage()
        record={**self.record,'status':status,'elapsedSeconds':round(max(0,time.monotonic()-self.started),3),'transferredBytes':max(0,int(transferred)),'outputBytes':max(0,int(output)),'attempt':attempt,'processCpuSeconds':round(max(0,cpu-self.before[0]),3),'processPeakRssBytes':rss}
        if code and isinstance(code,str) and len(code)<=64 and all(c.isalnum() or c=='_' for c in code):record['code']=code
        return record


def validate_record(record):
    if not isinstance(record,dict):raise ValueError('invalid metric')
    allowed={'id','at','mode','mediaType','format','status','elapsedSeconds','transferredBytes','outputBytes','attempt','processCpuSeconds','processPeakRssBytes','code'}
    if set(record)-allowed:raise ValueError('unexpected metric')
    if not isinstance(record.get('id'),str) or len(record['id'])>80:raise ValueError('invalid id')
    if record.get('mode') not in {'inline','worker'} or record.get('mediaType') not in {'audio','video'}:raise ValueError('invalid scope')
    if record.get('status') not in {'ready','failed','cancelled','recoverable_failure'}:raise ValueError('invalid status')
    if record.get('format') not in {'mp4','webm','mkv','mov','mp3','m4a','wav','flac','ogg','opus','aac','aiff'}:raise ValueError('invalid format')
    for key in ('at','elapsedSeconds','transferredBytes','outputBytes','attempt','processCpuSeconds','processPeakRssBytes'):
        value=record.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<2**53:raise ValueError('invalid measure')
    if 'code' in record and (not isinstance(record['code'],str) or len(record['code'])>64 or not all(c.isalnum() or c=='_' for c in record['code'])):raise ValueError('invalid code')
    return record


class BlobHistory:
    def __init__(self,credentials):self.credentials=credentials

    def path(self,user):
        # Same user validation as preferences; different namespace and schema.
        from .account import account_path
        account_path(user)
        return 'metrics/v1/'+hashlib.sha256(user.encode()).hexdigest()+'.json'

    def read(self,user):
        path=self.path(user)
        status,headers,raw=blob_http('GET',f'https://{self.credentials.store_id}.private.blob.vercel-storage.com/{path}?cache=0',headers={'Authorization':'Bearer '+self.credentials.token,'Accept-Encoding':'identity'},timeout=(1,2))
        if status==404:return [],None
        if status!=200:raise AudioError('O histórico está indisponível.','history_unavailable',503)
        try:
            document=parse_json(raw)
            if not isinstance(document,dict) or set(document)!={'schema','records'} or type(document['schema']) is not int or document['schema']!=1 or not isinstance(document['records'],list) or len(document['records'])>MAX_RECORDS:raise ValueError()
            records=[validate_record(row) for row in document['records']]
            etag=next((v for k,v in headers.items() if k.lower()=='etag'),'')
            if not etag or etag.startswith('W/') or len(etag)>256 or any(ord(c)<32 for c in etag):raise ValueError()
            return [r for r in records if r['at']>time.time()-RETENTION_SECONDS],etag
        except (KeyError,ValueError,TypeError):
            raise AudioError('O histórico está indisponível.','history_unavailable',503) from None

    def append(self,user,record):
        validate_record(record)
        for _ in range(3):
            records,etag=self.read(user)
            if any(row['id']==record['id'] for row in records):return
            records=sorted([record,*records],key=lambda r:r['at'],reverse=True)[:MAX_RECORDS]
            body=json.dumps({'schema':1,'records':records},separators=(',',':'),allow_nan=False).encode()
            while len(body)>32*1024:
                records.pop();body=json.dumps({'schema':1,'records':records},separators=(',',':')).encode()
            headers={'Authorization':'Bearer '+self.credentials.token,'x-vercel-blob-store-id':self.credentials.store_id,'x-api-version':BLOB_API_VERSION,'x-vercel-blob-access':'private','x-add-random-suffix':'0','x-allow-overwrite':'1' if etag else '0','x-content-type':'application/json','Content-Type':'application/json','x-cache-control-max-age':'60'}
            if etag:headers['x-if-match']=etag
            status,_,raw=blob_http('PUT',BLOB_API+'?'+urllib.parse.urlencode({'pathname':self.path(user)}),headers=headers,body=body,timeout=(1,2))
            if status in (200,201):return
            if status not in (409,412):raise AudioError('O histórico está indisponível.','history_unavailable',503)
        raise AudioError('O histórico está ocupado.','history_busy',503)


def persist(user,record):
    try:BlobHistory(BlobCredentials.from_environment()).append(user,record)
    except Exception:
        # Metrics must not make a completed download fail. No raw exceptions.
        import logging
        logging.getLogger('onda.metrics').warning('history_write_deferred code=history_unavailable')


def register_history(app):
    @app.get('/api/downloads/history')
    async def history(request:Request):
        user=user_identity(request)
        store=BlobHistory(BlobCredentials.from_environment())
        records,_=await asyncio.to_thread(store.read,user)
        return {'records':records,'windowDays':30,'maxRecords':MAX_RECORDS,'resourceScope':'process_observation'}
