"""Pinned Linux FFmpeg builds from a distributor linked by ffmpeg.org.

Only a dated release's stable maintenance branch is eligible. Builds are
hash-verified before extracting executable bytes; master/nightly is excluded.
No API or deployment credentials are attached to distribution requests.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import time
from urllib.parse import urljoin, urlsplit

API='https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=5'
LOCK='ffmpeg-runtime.json'
COMPONENT='ffmpeg-binary'
MAX_ARCHIVE=192*1024**2
MAX_BINARY=256*1024**2
HOSTS={'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
ASSET=re.compile(r'ffmpeg-(n(\d+\.\d+\.\d+)(?:-\d+-g[0-9a-f]+)?)-linux64-gpl-(\d+\.\d+)\.tar\.xz\Z')

class BinaryError(RuntimeError):pass

def validate(lock):
    try:
        origin=urlsplit(lock['url']);tag=lock['release'];match=ASSET.fullmatch(lock['asset'])
        if not re.fullmatch(r'autobuild-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}',tag) or not match:raise ValueError()
        if (lock.get('schema')!=1 or lock.get('provider')!='BtbN/FFmpeg-Builds'
                or origin.scheme!='https' or origin.hostname!='github.com' or origin.port not in (None,443)
                or origin.username or origin.password or origin.query or origin.fragment
                or origin.path!=f'/BtbN/FFmpeg-Builds/releases/download/{tag}/{lock["asset"]}'):raise ValueError()
        expected=match[1]+'-'+''.join(tag.split('-')[1:4])
        if lock['version']!=expected or match[2].rsplit('.',1)[0]!=match[3]:raise ValueError()
        if not re.fullmatch(r'[a-f0-9]{64}',lock['sha256']):raise ValueError()
        if type(lock['size']) is not int or not 0<lock['size']<=MAX_ARCHIVE:raise ValueError()
        if 'binarySha256' in lock and not re.fullmatch(r'[a-f0-9]{64}',lock['binarySha256']):raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):raise BinaryError('ffmpeg_manifest_invalid') from None
    return lock

def latest(http):
    from packaging.version import Version # Metadata watcher only, not the runtime installer.
    releases=http.json(API)
    if not isinstance(releases,list) or not releases:raise BinaryError('ffmpeg_release_unavailable')
    for release in releases:
        if not isinstance(release,dict):raise BinaryError('ffmpeg_release_unavailable')
        tag=release.get('tag_name','')
        if not re.fullmatch(r'autobuild-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}',tag) or release.get('draft'):continue
        if not isinstance(release.get('assets'),list) or len(release['assets'])>1000 or any(not isinstance(a,dict) for a in release['assets']):
            raise BinaryError('ffmpeg_release_unavailable')
        eligible=[(ASSET.fullmatch(a.get('name','')),a) for a in release.get('assets',[])]
        eligible=[(m,a) for m,a in eligible if m and m[2].rsplit('.',1)[0]==m[3]]
        if not eligible:continue
        match,asset=max(eligible,key=lambda pair:Version(pair[0][2]))
        digest=asset.get('digest','')
        if not digest.startswith('sha256:'):raise BinaryError('ffmpeg_digest_missing')
        return validate({'schema':1,'provider':'BtbN/FFmpeg-Builds','release':tag,'asset':asset['name'],
            'version':match[1]+'-'+''.join(tag.split('-')[1:4]),'url':asset['browser_download_url'],
            'sha256':digest[7:],'size':asset['size']})
    raise BinaryError('ffmpeg_stable_build_missing')

def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def extract(archive,target,lock):
    if archive.stat().st_size!=lock['size'] or digest(archive)!=lock['sha256']:
        raise BinaryError('ffmpeg_integrity_failed')
    found=False;members=0;total=0
    with tarfile.open(archive,'r:xz') as tar:
        for member in tar:
            members+=1;total+=member.size
            if members>2048 or total>1024**3:raise BinaryError('ffmpeg_archive_invalid')
            # Extract a single regular file, never paths, links, or devices.
            if not member.name.endswith('/bin/ffmpeg'):continue
            if found or not member.isfile() or not 0<member.size<=MAX_BINARY:
                raise BinaryError('ffmpeg_archive_invalid')
            found=True;source=tar.extractfile(member)
            with target.open('xb') as out:
                while block:=source.read(1024*1024):out.write(block)
    if not found:raise BinaryError('ffmpeg_archive_invalid')
    actual=digest(target)
    if lock.get('binarySha256',actual)!=actual:raise BinaryError('ffmpeg_integrity_failed')
    target.chmod(0o755)
    return {**lock,'binarySha256':actual}

def download(lock,target):
    import requests # The metadata-only watcher needs no download dependencies.
    validate(lock);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_file() and lock.get('binarySha256') and digest(target)==lock['binarySha256']:return lock
    deadline=time.monotonic()+300
    try:
        with tempfile.TemporaryDirectory(prefix='ffmpeg-',dir=target.parent) as temporary:
            archive=Path(temporary)/'archive.tar.xz';url=lock['url']
            with requests.Session() as session:
                session.headers.clear();session.headers['User-Agent']='Onda-AutoCura/3'
                class NoCredentials(requests.auth.AuthBase):
                    def __call__(self,request):
                        request.headers.pop('Authorization',None)
                        request.headers.pop('Cookie',None)
                        return request
                session.auth=NoCredentials() # Suppress netrc without disabling the network proxy.
                for hop in range(5):
                    parsed=urlsplit(url)
                    if parsed.scheme!='https' or parsed.hostname not in HOSTS or parsed.port not in (None,443) or parsed.username or parsed.password:
                        raise BinaryError('ffmpeg_untrusted_redirect')
                    response=session.get(url,stream=True,allow_redirects=False,timeout=(10,45))
                    if response.status_code in (301,302,303,307,308):
                        location=response.headers.get('Location','');response.close();url=urljoin(url,location);continue
                    with response:
                        if response.status_code!=200:raise BinaryError('ffmpeg_download_failed')
                        count=0
                        with archive.open('wb') as out:
                            for block in response.iter_content(1024*1024):
                                count+=len(block)
                                if count>lock['size'] or time.monotonic()>deadline:raise BinaryError('ffmpeg_download_failed')
                                out.write(block)
                    break
                else:raise BinaryError('ffmpeg_untrusted_redirect')
            pending=Path(temporary)/'ffmpeg';updated=extract(archive,pending,lock)
            os.replace(pending,target)
            return updated
    except (requests.RequestException,OSError,tarfile.TarError,EOFError):
        raise BinaryError('ffmpeg_download_failed') from None

def write_lock(root,lock):
    validate(lock);path=Path(root)/LOCK;path.parent.mkdir(exist_ok=True)
    pending=path.with_suffix('.json.pending');pending.write_text(json.dumps(lock,indent=2)+'\n');pending.replace(path)

def install(root):
    root=Path(root);lock=validate(json.loads((root/LOCK).read_text()))
    return download(lock,root/'bin'/'ffmpeg')

if __name__=='__main__':
    import sys
    install(Path(__file__).resolve().parents[1]);print('FFmpeg pinned binary verified')
