"""Trusted build-time executables; requests never select a binary path."""
from functools import lru_cache
from pathlib import Path
import os
import subprocess
import imageio_ffmpeg

ROOT=Path(__file__).resolve().parents[1]

@lru_cache(maxsize=1)
def ffmpeg_executable():
    bundled=ROOT/'bin'/'ffmpeg'
    if bundled.is_file():return str(bundled)
    if os.environ.get('VERCEL')=='1':raise RuntimeError('packaged_ffmpeg_missing')
    return imageio_ffmpeg.get_ffmpeg_exe()

@lru_cache(maxsize=1)
def ffmpeg_version():
    output=subprocess.check_output([ffmpeg_executable(),'-version'],text=True,stderr=subprocess.DEVNULL,timeout=5)
    return output.splitlines()[0].split()[2]
