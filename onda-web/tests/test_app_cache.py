"""No sensitive runtime caching; cache changes track the actual public release."""
from pathlib import Path
import shutil
import subprocess
import pytest
from scripts.build_app_cache import build

ROOT=Path(__file__).resolve().parents[1]

def test_only_public_assets_are_fingerprinted_and_version_is_reproducible(tmp_path):
    shutil.copytree(ROOT/'public',tmp_path/'public',ignore=shutil.ignore_patterns('*.mp4'))
    first,count,size=build(tmp_path)
    assert count>=20 and size<2*1024**2
    assert build(tmp_path)[0]==first
    css=tmp_path/'public/accessibility.css';css.write_text(css.read_text()+'\n/* owned change */\n')
    updated=build(tmp_path)[0];assert updated!=first and build(tmp_path)[0]==updated


def test_worker_never_caches_private_requests_or_mixed_release_assets():
    node=shutil.which('node')
    if not node:pytest.skip('Node is not installed in this API-only environment')
    result=subprocess.run([node,str(ROOT/'tests/app_worker_security.cjs')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
