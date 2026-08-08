from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .models import MediaInfo

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_supported_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and host in YOUTUBE_HOSTS and bool(parsed.path)
    except ValueError:
        return False


def safe_filename(value: str, fallback: str = "video") -> str:
    result = INVALID_FILENAME.sub("_", value).strip(" .")
    result = re.sub(r"\s+", " ", result)
    return result[:180] or fallback


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Duração desconhecida"
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


def has_free_space(destination: Path, estimated_bytes: int | None, reserve: int = 100 * 1024 * 1024) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination).free
    required = (estimated_bytes or 0) + reserve
    return free >= required


def format_bytes(value: int | None) -> str:
    if value is None or value < 0:
        return "tamanho desconhecido"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit in {"B", "KB"} else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def estimate_output_bytes(
    media: MediaInfo | None,
    media_type: str,
    output_format: str,
    quality: str,
) -> int | None:
    """Return a conservative display estimate, never a storage guarantee."""

    if not media or media.is_playlist:
        return None
    duration = max(0, int(media.duration or 0))
    if media_type == "audio":
        return round(duration * 192_000 / 8) if duration else None
    wanted_height = int(quality) if str(quality).isdigit() else None
    compatible = [
        item
        for item in media.formats
        if item.extension.casefold() == output_format.casefold()
        and (wanted_height is None or item.height is None or item.height <= wanted_height)
    ]
    known = [item for item in compatible if item.filesize]
    if known:
        selected = max(known, key=lambda item: item.height or 0)
        audio_allowance = round(duration * 160_000 / 8) if duration else 0
        return int(selected.filesize or 0) + audio_allowance
    if not duration:
        return None
    height = wanted_height or max((item.height or 0 for item in media.formats), default=1080) or 1080
    video_bitrate = 900_000 if height <= 480 else 2_500_000 if height <= 720 else 5_000_000 if height <= 1080 else 12_000_000 if height <= 1440 else 25_000_000
    return round(duration * (video_bitrate + 160_000) / 8)


def preview_output_name(title: str, media_id: str, extension: str, template: str) -> str:
    value = template or "%(title).180B [%(id)s].%(ext)s"
    replacements = {
        "%(title).180B": safe_filename(title),
        "%(title)s": safe_filename(title),
        "%(id)s": safe_filename(media_id, "media"),
        "%(ext)s": extension.casefold(),
    }
    for marker, replacement in replacements.items():
        value = value.replace(marker, replacement)
    value = re.sub(r"%\([^)]+\)[#0 +\-\d.]*[a-zA-Z]", "campo", value)
    return safe_filename(value, f"media.{extension.casefold()}")
