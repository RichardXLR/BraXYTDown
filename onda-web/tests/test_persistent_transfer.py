import hashlib
import io
from pathlib import Path
from yt_dlp.networking import Response
from api.direct_transfer import download_direct
from api.security import Guard


def test_persistent_resume_discards_only_uncheckpointed_tail(tmp_path):
    # A durable 8 MiB checkpoint survives a worker restart and avoids re-reading it.
    prefix=b'a'*(8*1024**2)
    tail=b'b'*1024
    source=tmp_path/'source.media';source.write_bytes(prefix+b'uncommitted-tail')
    guard=Guard(maximum_bytes=16*1024**2)
    url='https://public.example/owned.wav'
    guard.partial_checkpoint={'requestHash':hashlib.sha256(url.encode()).hexdigest(),'size':len(prefix),'sha256':hashlib.sha256(prefix).hexdigest(),'validator':['ETag','"same"'],'responseUrl':url,'total':len(prefix)+len(tail)}
    guard.save_partial=lambda _:None
    requests=[]
    class Handler:
        def __init__(self,guard):self.guard=guard
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def send(self,request):
            requests.append(request)
            return Response(io.BytesIO(tail),url,{'Content-Length':str(len(tail)),'Content-Range':f'bytes {len(prefix)}-{len(prefix)+len(tail)-1}/{len(prefix)+len(tail)}','ETag':'"same"'},206)
    path,details=download_direct(url,tmp_path,guard,handler_factory=Handler,verify_response=lambda *a:None,title_factory=lambda _: 'Own')
    assert requests[0].headers['Range']==f'bytes={len(prefix)}-'
    assert requests[0].headers['If-Range']=='"same"'
    assert path.stat().st_size==len(prefix)+len(tail)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==hashlib.sha256(prefix+tail).hexdigest()
    assert details['recovery']['resumed'] is True
