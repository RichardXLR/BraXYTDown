"""The executable supply chain and update channel are independent of PyPI."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import pytest
from scripts import ffmpeg_runtime as f
from scripts import autocura


def manifest():
    return {'schema':1,'provider':'BtbN/FFmpeg-Builds','release':'autobuild-2026-10-12-13-12',
        'asset':'ffmpeg-n9.0.2-27-g7e93014c5b-linux64-gpl-9.0.tar.xz',
        'version':'n9.0.2-27-g7e93014c5b-20261012',
        'url':'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-12-13-12/ffmpeg-n9.0.2-27-g7e93014c5b-linux64-gpl-9.0.tar.xz',
        'sha256':'a'*64,'size':4096}


@pytest.mark.parametrize('fault',['master','mutable','untrusted','digest','size','binary_digest','version'])
def test_untrusted_or_mutable_build_is_rejected(fault):
    value=manifest()
    if fault=='master':value['asset']='ffmpeg-master-linux64-gpl.tar.xz'
    elif fault=='mutable':value['release']='latest'
    elif fault=='untrusted':value['url']=value['url'].replace('github.com','evil.example')
    elif fault=='digest':value['sha256']='missing'
    elif fault=='size':value['size']=True
    elif fault=='binary_digest':value['binarySha256']='bad'
    else:value['version']='7.0.2'
    with pytest.raises(f.BinaryError):f.validate(value)


def test_latest_chooses_stable_dated_build_not_master():
    value=manifest()
    class Metadata:
        def json(self,url):
            assert url==f.API
            return [{'tag_name':'latest','assets':[]},{'tag_name':value['release'],'assets':[
                {'name':'ffmpeg-N-127388-g615b07170e-linux64-gpl.tar.xz','digest':'sha256:'+'b'*64},
                {'name':value['asset'],'digest':'sha256:'+value['sha256'],'size':value['size'],'browser_download_url':value['url']}]}]
    selected=f.latest(Metadata())
    assert selected['version']==value['version'] and selected['sha256']==value['sha256']


def archive(tmp_path,*,link=False,duplicate=False):
    path=tmp_path/'build.tar.xz'
    with tarfile.open(path,'w:xz') as t:
        info=tarfile.TarInfo('build/bin/ffmpeg')
        if link:info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd';t.addfile(info)
        else:
            data=b'verified fixture executable';info.size=len(data);t.addfile(info,io.BytesIO(data))
            if duplicate:t.addfile(info,io.BytesIO(data))
        # No arbitrary archive paths are ever extracted.
        info=tarfile.TarInfo('../../outside');info.size=4;t.addfile(info,io.BytesIO(b'nope'))
    value={**manifest(),'sha256':f.digest(path),'size':path.stat().st_size}
    return path,value


@pytest.mark.parametrize('fault',['digest','link','duplicate','binary'])
def test_corruption_or_links_never_install_executable(tmp_path,fault):
    path,value=archive(tmp_path,link=fault=='link',duplicate=fault=='duplicate')
    if fault=='digest':value['sha256']='f'*64
    if fault=='binary':value['binarySha256']='f'*64
    with pytest.raises(f.BinaryError):f.extract(path,tmp_path/'ffmpeg',value)
    assert not (tmp_path.parent/'outside').exists()


def test_verified_archive_extracts_single_binary_with_recorded_hash(tmp_path):
    path,value=archive(tmp_path);target=tmp_path/'ffmpeg'
    result=f.extract(path,target,value)
    assert result['binarySha256']==hashlib.sha256(target.read_bytes()).hexdigest()
    assert target.stat().st_mode&0o111 and len(list(tmp_path.iterdir()))==2


def test_binary_update_triggers_release_without_wrapper_update(tmp_path):
    class Dependencies:
        def latest(self):return {'imageio-ffmpeg':'0.6.0','ffmpeg-binary':'n9.0.2-new-build'}
    (tmp_path/'.autocura').mkdir()
    (tmp_path/'.autocura/state.json').write_text(json.dumps({'versions':{'imageio-ffmpeg':'0.6.0','ffmpeg-binary':'7.0.2'}}))
    result=autocura.check_updates(tmp_path,Dependencies())
    assert result['required'] and result['componentsChanged']==['ffmpeg-binary']


def test_candidate_version_gate_requires_exact_ffmpeg_binary():
    from tests.test_autocura import CanaryHTTP,NEW,VERSIONS
    checks=autocura.DeploymentChecks(CanaryHTTP(),canaries=[{'name':'owned','url':'https://onda-audio.vercel.app/canary.wav'}])
    result=checks.verify(NEW,versions={**VERSIONS,'ffmpeg-binary':'n9.0.2-new-build'})
    record=next(c for c in result['checks'] if c['name']=='packaged_versions')
    assert record['status']=='failed' and record['code']=='packaged_runtime_version_mismatch'


@pytest.mark.parametrize('failure',[None,'untrusted_redirect','corrupted_download'])
def test_download_verifies_hash_and_redirect_before_atomic_install(tmp_path,monkeypatch,failure):
    import requests
    file,value=archive(tmp_path);data=file.read_bytes();calls=[]
    target=tmp_path/'bin'/'ffmpeg'
    class Response:
        def __init__(self):
            self.status_code=302 if failure=='untrusted_redirect' else 200
            self.headers={'Location':'https://127.0.0.1/private'}
        def close(self):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,size):yield b'X'*len(data) if failure=='corrupted_download' else data
    class Session:
        def __init__(self):self.headers={};self.auth=None
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**options):
            assert self.auth and options['allow_redirects'] is False
            assert 'Authorization' not in self.headers
            calls.append(url);return Response()
    monkeypatch.setattr(requests,'Session',Session)
    if failure:
        with pytest.raises(f.BinaryError):f.download(value,target)
        assert not target.exists() and len(calls)==1
    else:
        installed=f.download(value,target)
        assert target.exists() and installed['binarySha256']==f.digest(target)


def test_failed_candidate_restores_ffmpeg_manifest_alongside_python_pins(tmp_path):
    from tests.test_autocura import factory
    make=factory.__wrapped__(tmp_path)
    manager,provider,events,records=make(('passed','failed'))
    lock=tmp_path/f.LOCK;lock.write_text(json.dumps(manifest()));original=lock.read_bytes()
    prepare=manager.dependencies.prepare
    def update(versions):
        prepare(versions);lock.write_text('{"unconfirmed":"candidate"}')
    manager.dependencies.prepare=update
    manager.release()
    assert lock.read_bytes()==original
