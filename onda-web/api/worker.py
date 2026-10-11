"""One bounded worker process with fenced leases, checkpoints and disk-backed output."""
from __future__ import annotations
import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import threading
import time
from .engine import prepare_download, MediaSettings, SessionOptions, VideoSources, FORMATS, VIDEO_FORMATS
from .jobs import local_store
from .resources import ResourcePolicy, register_workspace, release_workspace
from .security import Guard, AudioError
from .recovery import retry_kind

LOGGER = logging.getLogger('onda.worker')


def sha256(path, guard=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024*1024):
            if guard:
                guard.check()
            digest.update(block)
    return digest.hexdigest()


class JobGuard(Guard):
    def __init__(self, store, row, **kwargs):
        super().__init__(**kwargs)
        self.store, self.job, self.fence = store,row['id'],row['fence']
        self.last_lease = 0
        self.last_progress = 0
        self.totals, self.transfers = {}, {}
        self.progress_lock = threading.Lock()
        self.transfer_started = time.monotonic()
        self.progress = self
        self.partition = row['owner']

    def check(self):
        super().check()
        if time.monotonic()-self.last_lease>.5:
            row = self.store.get(self.job)
            if row['status']!='running' or row['fence']!=self.fence:
                self.abort()
                raise AudioError('Tarefa cancelada ou reassumida por outro worker.', 'cancelled',499)
            self.last_lease = time.monotonic()
            self.store.update(self.job,self.fence)
            self.store.heartbeat()

    def emit(self, stage, **values):
        if time.monotonic()-self.last_progress<.25 and not values.get('force'):
            return
        safe = {'stage':stage}
        for key in ('downloadedBytes','totalBytes','speedBytesPerSecond','processedSeconds','durationSeconds','outputBytes','attempt'):
            value = values.get(key)
            if type(value) in (int,float) and 0<=value<2**53:
                safe[key]=value
        self.store.update(self.job,self.fence,progress=safe)
        self.last_progress=time.monotonic()

    def begin_download(self, totals, *, offsets=None, attempt=1):
        self.totals=totals
        self.transfers=dict(offsets or {})
        self.transfer_started=time.monotonic()
        self.emit('downloading',attempt=attempt,force=True)

    def transfer(self, identity, downloaded, total=None, **kwargs):
        self.transfers[identity]=downloaded
        if total is not None:
            self.totals[identity]=total
        known=list(self.totals.values())
        size=sum(known) if known and all(type(v) in (int,float) and v>0 for v in known) else None
        self.emit('downloading',downloadedBytes=sum(self.transfers.values()),totalBytes=size,attempt=kwargs.get('attempt',1))


def process_job(store, row):
    policy=ResourcePolicy.from_environment()
    directory=store.root/row['id']
    directory.mkdir(mode=0o700,exist_ok=True)
    lock = os.open(directory / '.worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        if store.update(row['id'],row['fence'],status='queued'):
            with store.connect() as db:
                db.execute("UPDATE jobs SET attempts=max(0,attempts-1) WHERE id=? AND fence=? AND status='queued'",(row['id'],row['fence']))
        time.sleep(.5)
        return
    register_workspace(directory)
    guard=JobGuard(store,row,seconds=policy.process_seconds,maximum_bytes=policy.max_transfer_bytes,resource_policy=policy,workspace=directory)
    try:
        guard.emit('extracting',force=True)
        data=store.open(row['id'],row['payload'])
        guard.acquired_checkpoint=None
        guard.partial_checkpoint=None
        if row['checkpoint']:
            checkpoint=store.open(row['id'],row['checkpoint'])
            valid=checkpoint.get('kind')=='acquired'
            if checkpoint.get('kind')=='direct':
                guard.partial_checkpoint=checkpoint
            for item in checkpoint.get('inputs',[]):
                path=(directory/item['path']).resolve()
                if directory not in path.parents or not path.is_file() or sha256(path,guard)!=item['sha256']:
                    valid=False
                    break
            if valid:
                guard.acquired_checkpoint=checkpoint

        def checkpoint(sources, details):
            paths=[sources.video,sources.audio] if isinstance(sources,VideoSources) else [sources]
            value={'kind':'acquired','video':isinstance(sources,VideoSources),'inputs':[], 'details':details}
            for path in paths:
                if path:
                    value['inputs'].append({'path':str(path.relative_to(directory)),'sha256':sha256(path,guard)})
            store.update(row['id'],row['fence'],checkpoint=value)
        guard.save_partial=lambda value: store.update(row['id'],row['fence'],checkpoint=value)
        guard.save_acquired=checkpoint
        settings=MediaSettings(**{k:data[k] for k in MediaSettings.__dataclass_fields__})
        session=SessionOptions(video_password=data.get('video_password'),user_agent=data.get('user_agent'))
        target,details=prepare_download(data['url'],directory,data['format'],data['quality'],guard,cookies=data.get('cookies'),settings=settings,session=session)
        guard.emit('verifying',force=True)
        digest=sha256(target,guard)
        size=target.stat().st_size
        guard.check_output(size)
        mime=VIDEO_FORMATS[data['format']][2] if data['media_type']=='video' else FORMATS[data['format']][1]
        import re
        title=re.sub(r'[\x00-\x1f\x7f/\\]','',str(details.get('title') or 'Mídia'))[:160]
        file={'path':target.name,'filename':f'{title}.{data["format"]}','size':size,'mimeType':mime,'sha256':digest,'etag':'"'+digest+'"','mtimeNs':target.stat().st_mtime_ns}
        guard.check()
        os.chmod(target,0o400)
        if store.update(row['id'],row['fence'],status='ready',file=file,progress={'stage':'ready','downloadedBytes':size,'totalBytes':size}):
            for item in directory.iterdir():
                if item!=target:
                    if item.is_dir():
                        shutil.rmtree(item,ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
    except Exception as exc:
        code=exc.code if isinstance(exc,AudioError) else 'worker_failed'
        current=store.get(row['id'])
        if current['status']=='running' and current['fence']==row['fence']:
            if retry_kind(exc)=='transient' and row['attempts']<3:
                # Keep acquired input checkpoint; bounded retries remain visible.
                store.update(row['id'],row['fence'],status='queued',progress={'stage':'recoverable_failure','code':code})
                time.sleep(min(4,2**row['attempts']))
            else:
                store.update(row['id'],row['fence'],status='failed',progress={'stage':'failed','code':code})
                shutil.rmtree(directory,ignore_errors=True)
        elif current['status']=='cancelled':
            shutil.rmtree(directory,ignore_errors=True)
        LOGGER.warning('job_failure code=%s',code)
    finally:
        guard.abort()
        release_workspace(directory)
        os.close(lock)


def main():
    store=local_store()
    if not store:
        raise SystemExit('Configure ONDA_WORKER_MODE, ONDA_JOB_DIR em um volume persistente e ONDA_JOB_SECRET (32 bytes em base64).')
    stopping=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopping.set())
    signal.signal(signal.SIGINT,lambda *_:stopping.set())
    last_cleanup=0
    while not stopping.is_set():
        store.heartbeat()
        if time.monotonic()-last_cleanup>60:
            store.cleanup()
            last_cleanup=time.monotonic()
        row=store.claim()
        if row:
            process_job(store,row)
        else:
            stopping.wait(1)
