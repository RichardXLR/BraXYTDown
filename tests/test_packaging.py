from pathlib import Path
import tomllib

from baixatube import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_packaging_versions_stay_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    installer = (ROOT / "installer" / "BaixaTube.iss").read_text(encoding="utf-8")
    version_info = (ROOT / "installer" / "version_info.txt").read_text(encoding="utf-8")
    assert project["project"]["name"] == "braxytdow"
    assert project["project"]["version"] == __version__ == "3.2.0"
    assert '#define MyAppName "BraXYTDow"' in installer
    assert '#define MyAppVersion "3.2.0"' in installer
    assert '#define MyAppPublisher "Richard Ittou"' in installer
    assert "FileVersion', '3.2.0.0" in version_info
    assert "filevers=(3, 2, 0, 0)" in version_info
    assert "OriginalFilename', 'BraXYTDow.exe" in version_info


def test_specs_use_stable_launcher_and_bundle_release_tools():
    for spec_name in ("BaixaTube.spec", "BaixaTube-onedir.spec"):
        spec = (ROOT / spec_name).read_text(encoding="utf-8").replace("\\", "/")
        assert 'root / "baixatube_launcher.py"' in spec
        assert 'baixatube" / "__main__.py"' not in spec
        for tool in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe"):
            assert f'bin/{tool}' in spec
        assert "assets/baixatube-icon.png" in spec
        assert "assets/creator-richard.jpg" in spec
        assert "assets/instagram.svg" in spec
        assert "baixatube.ico" in spec
        assert 'name="BraXYTDow"' in spec


def test_third_party_notices_cover_bundled_executables():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
    for component in ("yt-dlp", "ffmpeg", "ffprobe", "deno", "pyside6", "pyinstaller"):
        assert component in notices
    assert "gpl-3.0" in notices
    assert "sha-256" in notices
