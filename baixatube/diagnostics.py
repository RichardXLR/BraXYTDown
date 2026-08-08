from __future__ import annotations

import logging
import platform
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .branding import APP_NAME, CREATOR_NAME
from .paths import data_dir, find_binary, legacy_data_dir


_DATA_ROOT = data_dir()
LOG_PATH = _DATA_ROOT / ("baixatube.log" if _DATA_ROOT.resolve() == legacy_data_dir().resolve() else "braxytdow.log")


def configure_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(item, RotatingFileHandler) for item in root.handlers):
        return
    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


def binary_version(name: str) -> str:
    binary = find_binary(name)
    if not binary:
        return "não encontrado"
    args = [binary, "--version"] if name == "yt-dlp" else [binary, "-version"]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        first = (completed.stdout or completed.stderr).splitlines()[0]
        return first.strip()[:180]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "não foi possível consultar"


def diagnostic_text() -> str:
    return "\n".join(
        [
            f"{APP_NAME}: {__version__}",
            f"Criador: {CREATOR_NAME}",
            f"Python: {platform.python_version()}",
            f"Sistema: {platform.platform()}",
            f"Executável: {sys.executable}",
            f"yt-dlp: {binary_version('yt-dlp')}",
            f"FFmpeg: {binary_version('ffmpeg')}",
            f"Dados: {data_dir()}",
            f"Log: {LOG_PATH}",
        ]
    )
