"""Keep Deno's wheel-installed executable inside the Python function bundle."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess

import deno
import imageio_ffmpeg
from yt_dlp.version import __version__ as ytdlp_version


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    executable = Path(deno.find_deno_bin())
    target = root / "bin" / "deno"
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(executable, target)
    target.chmod(0o755)
    version = subprocess.check_output([str(target), "--version"], text=True, timeout=10)
    print(f"Bundled {version.splitlines()[0]} ({target.stat().st_size // 1024 // 1024} MB)")
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    manifest = {
        "schemaVersion": 1,
        "versions": {"ytDlp": ytdlp_version, "deno": version.splitlines()[0].split()[1],
                     "ffmpeg": imageio_ffmpeg.get_ffmpeg_version()},
        "sha256": {"deno": digest(target), "ffmpeg": digest(ffmpeg)},
    }
    pending = root / "toolchain.json.pending"
    pending.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    pending.replace(root / "toolchain.json")
    # public/ is served separately by the CDN and is absent from the Function.
    # Keep this non-secret build snapshot alongside the bundled tools.
    report = root / "public" / "autocura.json"
    if report.is_file():
        shutil.copyfile(report, root / "autocura-report.json")


if __name__ == "__main__":
    main()
