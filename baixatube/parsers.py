from __future__ import annotations

import json
import re
from typing import Any

from .models import Chapter, DownloadProgress, DownloadStatus, FormatOption, MediaInfo, PlaylistItem


def parse_media_json(payload: str) -> MediaInfo:
    data: dict[str, Any] = json.loads(payload)
    entries_data = data.get("entries") or []
    is_playlist = data.get("_type") in {"playlist", "multi_video"} or bool(entries_data)
    entries = []
    for item in entries_data:
        if not item:
            continue
        video_id = str(item.get("id") or "")
        url = item.get("webpage_url") or item.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
        entries.append(
            PlaylistItem(
                video_id,
                item.get("title") or "Sem título",
                url,
                item.get("duration"),
                playlist_index=item.get("playlist_index") or item.get("playlist_autonumber"),
                uploader=item.get("uploader") or item.get("channel") or "",
                upload_date=item.get("upload_date") or "",
            )
        )
    formats = []
    seen: set[tuple[int | None, str]] = set()
    for item in data.get("formats") or []:
        height, ext = item.get("height"), item.get("ext") or ""
        if item.get("vcodec") == "none" or (height, ext) in seen:
            continue
        seen.add((height, ext))
        label = f"{height}p · {ext.upper()}" if height else ext.upper()
        formats.append(FormatOption(str(item.get("format_id", "")), label, ext, height, item.get("filesize") or item.get("filesize_approx")))
    formats.sort(key=lambda value: value.height or 0, reverse=True)
    chapters = []
    for item in data.get("chapters") or []:
        try:
            start = float(item.get("start_time", 0))
            raw_end = item.get("end_time")
            end = float(raw_end) if raw_end is not None else None
        except (TypeError, ValueError):
            continue
        chapters.append(Chapter(str(item.get("title") or f"Capítulo {len(chapters) + 1}"), start, end))
    return MediaInfo(
        id=str(data.get("id") or ""), title=data.get("title") or "Sem título",
        webpage_url=data.get("webpage_url") or data.get("original_url") or "",
        uploader=data.get("uploader") or data.get("channel") or "",
        duration=data.get("duration"), thumbnail=data.get("thumbnail") or "",
        is_playlist=is_playlist, formats=formats, entries=entries,
        subtitles=sorted(set((data.get("subtitles") or {}) | (data.get("automatic_captions") or {}))),
        chapters=chapters,
        upload_date=data.get("upload_date") or "",
        playlist_id=str(data.get("playlist_id") or (data.get("id") if is_playlist else "") or ""),
        playlist_title=str(data.get("playlist_title") or (data.get("title") if is_playlist else "") or ""),
    )


def parse_progress_line(line: str) -> DownloadProgress | None:
    line = line.strip()
    if line.startswith("BT_FILE:"):
        return DownloadProgress(DownloadStatus.COMPLETED, 100, message=line[8:])
    if not line.startswith("BT_PROGRESS|"):
        if "[Merger]" in line or "[ExtractAudio]" in line or "[Metadata]" in line or "[EmbedThumbnail]" in line:
            return DownloadProgress(DownloadStatus.CONVERTING, message=line)
        return None
    fields = (line.split("|") + [""] * 6)[:6]
    match = re.search(r"([\d.,]+)", fields[1])
    percent = float(match.group(1).replace(",", ".")) if match else 0.0
    return DownloadProgress(DownloadStatus.DOWNLOADING, percent, fields[2].strip(), fields[3].strip(), fields[4].strip(), fields[5].strip())
