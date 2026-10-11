"""Reproducible owned-fixture benchmark. Disk I/O is not Internet throughput."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time
import wave

parser=argparse.ArgumentParser()
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--size-mib',type=int,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
sys.path.insert(0,str(args.root.resolve()))
from api import engine,security
from yt_dlp.networking import Response

work=Path(tempfile.mkdtemp(prefix='onda-benchmark-'))
source=work/'owned-source.wav'
# A real PCM file with bounded allocation; reserve 128 bytes for container headers.
size=args.size_mib*1024**2-128
with wave.open(str(source),'wb') as output:
    output.setnchannels(2);output.setsampwidth(2);output.setframerate(44100)
    remaining=size-44
    block=b'\x00'*(1024*1024)
    while remaining:
        amount=min(remaining,len(block));output.writeframesraw(block[:amount]);remaining-=amount

class FixtureTransport:
    def __init__(self,guard):self.guard=guard
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def send(self,request):
        response=Response(source.open('rb'),request.url,{'Content-Type':'audio/wav','Content-Length':str(source.stat().st_size),'ETag':'"owned-fixture"'},200)
        return security.BoundedResponse(response,self.guard)

engine.direct_handler=FixtureTransport
engine.public_url=lambda *a,**kw:('owned.example',[])
guard=security.Guard(seconds=240)
if hasattr(security.Guard,'check_output'):
    from api.resources import ResourcePolicy,register_workspace,release_workspace
    guard.maximum_bytes=12*1024**3
    guard.resource_policy=ResourcePolicy(mode='worker',max_source_bytes=4*1024**3,max_output_bytes=4*1024**3,max_transfer_bytes=12*1024**3,max_workspace_bytes=8*1024**3,process_seconds=1800,total_seconds=1860)
    guard.workspace=work/'task';guard.workspace.mkdir();register_workspace(guard.workspace)
else:
    guard.workspace=work/'task';guard.workspace.mkdir()

def pcm_digest(path):
    digest=hashlib.sha256()
    with wave.open(str(path),'rb') as media:
        while block:=media.readframes(16384):digest.update(block)
    return digest.hexdigest()

before=resource.getrusage(resource.RUSAGE_SELF)
children=resource.getrusage(resource.RUSAGE_CHILDREN)
started=time.monotonic()
result={'nominalMiB':args.size_mib,'sourceBytes':source.stat().st_size,'fixture':'owned PCM WAV, no network socket','profile':'persistent worker resource budget' if hasattr(security.Guard,'check_output') else 'published baseline'}
try:
    target,details=engine.prepare_download('https://owned.example/own.wav',guard.workspace,'wav','source',guard)
    elapsed=time.monotonic()-started
    valid=engine.probe_media(target,guard)['audio'] and pcm_digest(source)==pcm_digest(target)
    result.update(completed=True,outputBytes=target.stat().st_size,integrityVerified=bool(valid),sha256=hashlib.file_digest(target.open('rb'),'sha256').hexdigest(),elapsedSeconds=elapsed,localPipelineMiBPerSecond=source.stat().st_size/1024**2/elapsed)
except Exception as exc:
    result.update(completed=False,errorCode=getattr(exc,'code',type(exc).__name__),elapsedSeconds=time.monotonic()-started)
finally:
    after=resource.getrusage(resource.RUSAGE_SELF);child_after=resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update(pythonPeakRSSMiB=after.ru_maxrss/1024,childPeakRSSMiB=child_after.ru_maxrss/1024,cpuSeconds=(after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime)+(child_after.ru_utime+child_after.ru_stime-children.ru_utime-children.ru_stime),transferredBytes=guard.received)
    if hasattr(security.Guard,'check_output'):release_workspace(guard.workspace)
    guard.abort();shutil.rmtree(work)
args.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='sha256'}))
