from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any


class DownloadStatus(StrEnum):
    WAITING = "aguardando"
    ANALYZING = "analisando"
    DOWNLOADING = "baixando"
    CONVERTING = "convertendo"
    COMPLETED = "concluído"
    CANCELLED = "cancelado"
    ERROR = "erro"


@dataclass(slots=True)
class FormatOption:
    format_id: str
    label: str
    extension: str
    height: int | None = None
    filesize: int | None = None


@dataclass(slots=True)
class PlaylistItem:
    id: str
    title: str
    url: str
    duration: int | None = None
    selected: bool = True
    playlist_index: int | None = None
    uploader: str = ""
    upload_date: str = ""


@dataclass(slots=True)
class Chapter:
    title: str
    start_time: float
    end_time: float | None = None


@dataclass(slots=True)
class MediaInfo:
    id: str
    title: str
    webpage_url: str
    uploader: str = ""
    duration: int | None = None
    thumbnail: str = ""
    is_playlist: bool = False
    formats: list[FormatOption] = field(default_factory=list)
    entries: list[PlaylistItem] = field(default_factory=list)
    subtitles: list[str] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    upload_date: str = ""
    playlist_id: str = ""
    playlist_title: str = ""


@dataclass(slots=True)
class DownloadRequest:
    id: str
    url: str
    title: str
    destination: str
    media_type: str = "video"
    output_format: str = "mp4"
    quality: str = "auto"
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    download_subtitles: bool = False
    playlist_items: list[str] = field(default_factory=list)
    playlist_media_ids: list[str] = field(default_factory=list)
    subtitle_languages: str = "pt.*,en.*"
    embed_subtitles: bool = True
    audio_quality: str = "0"
    filename_template: str = "%(title).180B [%(id)s].%(ext)s"
    concurrent_fragments: int = 4
    retries: int = 10
    rate_limit: str = ""
    write_description: bool = False
    write_info_json: bool = False
    sponsorblock: bool = False
    trim_start: str = ""
    trim_end: str = ""
    playlist_reverse: bool = False
    overwrite: bool = False
    media_id: str = ""
    split_chapters: bool = False
    selected_chapters: list[dict[str, Any]] = field(default_factory=list)
    priority: int = 1
    order_index: int = 0
    scheduled_at: str = ""
    pause_on_metered: bool = False
    po_token_provider: bool = False
    cookie_mode: str = "off"
    cookie_browser: str = "edge"
    cookie_profile: str = ""
    cookie_file: str = ""
    cookie_consent: bool = False
    custom_filename: str = ""
    custom_title: str = ""
    custom_artist: str = ""
    custom_album: str = ""
    custom_cover: str = ""
    uploader: str = ""
    duration: int | None = None
    upload_date: str = ""
    playlist_id: str = ""
    playlist_title: str = ""
    playlist_index: int | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DownloadRequest":
        # Ignore fields written by a newer version while supplying defaults for
        # fields introduced after the row was persisted.
        known = {item.name for item in fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in known})

    @property
    def destination_path(self) -> Path:
        return Path(self.destination)


@dataclass(slots=True)
class DownloadProgress:
    status: DownloadStatus
    percent: float = 0.0
    speed: str = ""
    eta: str = ""
    downloaded: str = ""
    total: str = ""
    message: str = ""


@dataclass(slots=True)
class HistoryEntry:
    request_id: str
    title: str
    url: str
    output_path: str
    status: str
    created_at: str
    media_id: str = ""
    uploader: str = ""
    duration: int | None = None
    media_type: str = "video"
    output_format: str = ""
    quality: str = ""
    playlist_id: str = ""
    playlist_title: str = ""
    playlist_index: int | None = None
    file_size: int = 0
    file_state: str = "unknown"
