"""Owned 4K CBR fixture: compare processing, copying and preserved video packets."""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time
parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--fixture',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
sys.path.insert(0,str(args.root.resolve()))
from api import engine,security
import imageio_ffmpeg
work=Path(tempfile.mkdtemp(prefix='onda-video-benchmark-'))
source=work/'source.mp4';shutil.copyfile(args.fixture,source)
guard=security.Guard()
if hasattr(guard,'check_output'):
 from api.resources import ResourcePolicy,register_workspace,release_workspace
 guard.resource_policy=ResourcePolicy();guard.workspace=work;guard.maximum_bytes=guard.resource_policy.max_transfer_bytes;register_workspace(work)
before=resource.getrusage(resource.RUSAGE_CHILDREN);started=time.monotonic();record={'sourceBytes':source.stat().st_size,'fixture':'own generated 3840x2160 H.264 at 24fps with CBR filler, 2 seconds'}
def stream_hash(path):
 return subprocess.check_output([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-i',str(path),'-map','0:v:0','-c','copy','-f','hash','-hash','sha256','-'],text=True,timeout=30).strip()
try:
 target=engine.convert_video(source,work,'mp4','source',guard,engine.MediaSettings(media_type='video'))
 elapsed=time.monotonic()-started;after=resource.getrusage(resource.RUSAGE_CHILDREN)
 record.update(completed=True,elapsedSeconds=elapsed,ffmpegCPUSeconds=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,outputBytes=target.stat().st_size,copiedVideo=getattr(guard,'copy_attempted',False),encodedVideoPacketsPreserved=stream_hash(source)==stream_hash(target),peakChildRSSMiB=after.ru_maxrss/1024)
except Exception as exc:record.update(completed=False,errorCode=getattr(exc,'code',type(exc).__name__))
finally:
 guard.abort()
 if hasattr(guard,'check_output'):release_workspace(work)
 shutil.rmtree(work)
args.output.write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
