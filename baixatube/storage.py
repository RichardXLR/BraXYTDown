from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import DownloadRequest, DownloadStatus, HistoryEntry
from .paths import data_dir, legacy_data_dir

logger = logging.getLogger(__name__)


DEFAULT_SETTINGS = {
    "destination": str(Path.home() / "Downloads"),
    "format": "mp4",
    "quality": "auto",
    "concurrency": 2,
    "subtitles": False,
    "metadata": True,
    "thumbnail": True,
    "open_after": False,
    "completion_action": "notify",
    "interface_animations": True,
    "auto_update_tools": True,
    "update_channel": "nightly",
    "last_update_check": "",
    "app_update_manifest_url": "",
    "app_update_channel": "stable",
    "installation_id": "",
    "filename_template": "%(title).180B [%(id)s].%(ext)s",
    "audio_quality": "0",
    "subtitle_languages": "pt.*,en.*",
    "concurrent_fragments": 4,
    "retries": 10,
    "rate_limit": "",
    "write_description": False,
    "write_info_json": False,
    "sponsorblock": False,
    "split_chapters": False,
    "custom_presets": [],
    "preferred_preset": "manual",
    "po_token_provider": False,
    "po_token_manifest_url": "",
    "cookie_mode": "off",
    "cookie_browser": "edge",
    "cookie_profile": "",
    "cookie_file": "",
    "cookie_consent": False,
    "remote_ejs_fallback": True,
    "pause_on_metered": False,
    "close_to_tray": True,
    "native_notifications": True,
    "large_text": False,
    "high_contrast": False,
    "library_only_new": True,
    "bandwidth_day": "",
    "bandwidth_night": "",
    "bandwidth_night_start": 22,
    "bandwidth_night_end": 7,
}


def migrate_legacy_database(destination: Path, source: Path | None = None) -> bool:
    """Atomically copy the former database when both brand folders exist."""

    source = source or (legacy_data_dir() / "baixatube.db")
    if destination.exists() or not source.is_file() or source.resolve() == destination.resolve():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".migrate", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        legacy_db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10)
        migrated_db = sqlite3.connect(temporary, timeout=10)
        try:
            legacy_db.backup(migrated_db)
            migrated_db.commit()
        finally:
            migrated_db.close()
            legacy_db.close()
        os.replace(temporary, destination)
        logger.info("Dados da versão anterior migrados para %s", destination)
        return True
    except (OSError, sqlite3.Error):
        logger.warning("Não foi possível migrar o banco da versão anterior", exc_info=True)
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class Storage:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            root = data_dir()
            old_database = legacy_data_dir() / "baixatube.db"
            if root.resolve() == legacy_data_dir().resolve() and old_database.is_file():
                self.path = old_database
            else:
                self.path = root / "braxytdow.db"
                migrate_legacy_database(self.path, old_database)
        else:
            self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queue (
                    request_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS queue_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    payload TEXT NOT NULL,
                    error TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS download_archive (
                    media_key TEXT PRIMARY KEY,
                    media_id TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    uploader TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    output_format TEXT NOT NULL DEFAULT '',
                    playlist_id TEXT NOT NULL DEFAULT '',
                    playlist_title TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            history_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(history)")}
            migrations = {
                "media_id": "TEXT NOT NULL DEFAULT ''",
                "uploader": "TEXT NOT NULL DEFAULT ''",
                "duration": "INTEGER",
                "media_type": "TEXT NOT NULL DEFAULT 'video'",
                "output_format": "TEXT NOT NULL DEFAULT ''",
                "quality": "TEXT NOT NULL DEFAULT ''",
                "playlist_id": "TEXT NOT NULL DEFAULT ''",
                "playlist_title": "TEXT NOT NULL DEFAULT ''",
                "playlist_index": "INTEGER",
                "file_size": "INTEGER NOT NULL DEFAULT 0",
                "file_state": "TEXT NOT NULL DEFAULT 'unknown'",
            }
            for column, definition in migrations.items():
                if column not in history_columns:
                    db.execute(f"ALTER TABLE history ADD COLUMN {column} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS idx_history_media_id ON history(media_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_history_status_created ON history(status, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_archive_playlist ON download_archive(playlist_id, media_id)")
            db.execute("PRAGMA journal_mode=WAL")

    def settings(self) -> dict:
        values = DEFAULT_SETTINGS.copy()
        with self.connect() as db:
            for row in db.execute("SELECT key, value FROM settings"):
                try:
                    values[row["key"]] = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    # Ignore a damaged preference instead of preventing the UI
                    # from starting; saving the dialog replaces it later.
                    continue
        if not str(values.get("installation_id", "")).strip():
            values["installation_id"] = str(uuid.uuid4())
            self.save_settings({"installation_id": values["installation_id"]})
        return values

    def save_settings(self, values: dict) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()],
            )

    def save_queue_item(self, request: DownloadRequest, status: DownloadStatus) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO queue(request_id, payload, status) VALUES (?, ?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET payload=excluded.payload, "
                "status=excluded.status, updated_at=CURRENT_TIMESTAMP",
                (request.id, json.dumps(request.to_dict(), ensure_ascii=False), status.value),
            )

    def remove_queue_item(self, request_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM queue WHERE request_id=?", (request_id,))

    def recoverable_queue(self) -> list[DownloadRequest]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT request_id, payload FROM queue WHERE status NOT IN (?, ?)",
                (DownloadStatus.COMPLETED.value, DownloadStatus.CANCELLED.value),
            ).fetchall()
            recovered: list[DownloadRequest] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    if not isinstance(payload, dict):
                        raise TypeError("o payload não é um objeto JSON")
                    recovered.append(DownloadRequest.from_dict(payload))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    logger.warning("Item %s da fila foi colocado em quarentena: %s", row["request_id"], exc)
                    db.execute(
                        "INSERT INTO queue_errors(request_id, payload, error) VALUES (?, ?, ?)",
                        (row["request_id"], row["payload"], str(exc)[:500]),
                    )
                    db.execute("DELETE FROM queue WHERE request_id=?", (row["request_id"],))
        recovered.sort(key=lambda item: (-max(0, min(2, int(item.priority))), int(item.order_index), item.created_at, item.id))
        return recovered

    def save_queue_order(self, request_ids: list[str]) -> None:
        with self.connect() as db:
            for index, request_id in enumerate(request_ids):
                row = db.execute("SELECT payload, status FROM queue WHERE request_id=?", (request_id,)).fetchone()
                if not row:
                    continue
                try:
                    payload = json.loads(row["payload"])
                    payload["order_index"] = index
                    db.execute(
                        "UPDATE queue SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE request_id=?",
                        (json.dumps(payload, ensure_ascii=False), request_id),
                    )
                except (json.JSONDecodeError, TypeError):
                    continue

    def add_history(self, entry: HistoryEntry) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO history(request_id, title, url, output_path, status, created_at, media_id, "
                "uploader, duration, media_type, output_format, quality, playlist_id, playlist_title, "
                "playlist_index, file_size, file_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.request_id,
                    entry.title,
                    entry.url,
                    entry.output_path,
                    entry.status,
                    entry.created_at,
                    entry.media_id,
                    entry.uploader,
                    entry.duration,
                    entry.media_type,
                    entry.output_format,
                    entry.quality,
                    entry.playlist_id,
                    entry.playlist_title,
                    entry.playlist_index,
                    entry.file_size,
                    entry.file_state,
                ),
            )
            if entry.status == DownloadStatus.COMPLETED.value:
                media_key = entry.media_id.strip() or entry.url.strip()
                if media_key:
                    db.execute(
                        "INSERT INTO download_archive(media_key, media_id, url, title, uploader, output_path, "
                        "output_format, playlist_id, playlist_title, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(media_key) DO UPDATE SET url=excluded.url, title=excluded.title, "
                        "uploader=excluded.uploader, output_path=excluded.output_path, output_format=excluded.output_format, "
                        "playlist_id=excluded.playlist_id, playlist_title=excluded.playlist_title, "
                        "completed_at=excluded.completed_at, last_seen_at=CURRENT_TIMESTAMP",
                        (
                            media_key,
                            entry.media_id,
                            entry.url,
                            entry.title,
                            entry.uploader,
                            entry.output_path,
                            entry.output_format,
                            entry.playlist_id,
                            entry.playlist_title,
                            entry.created_at,
                        ),
                    )

    def history(self, limit: int = 100, search: str = "", status: str = "") -> list[HistoryEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if search.strip():
            clauses.append("(title LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(1000, limit)))
        with self.connect() as db:
            rows = db.execute(
                "SELECT request_id, title, url, output_path, status, created_at, media_id, uploader, duration, "
                "media_type, output_format, quality, playlist_id, playlist_title, playlist_index, file_size, file_state "
                f"FROM history{where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [HistoryEntry(**dict(row)) for row in rows]

    def completed_duplicate(self, media_id: str, url: str) -> HistoryEntry | None:
        """Return the most recent completed copy of the same public media."""

        clauses = []
        params: list[object] = [DownloadStatus.COMPLETED.value]
        if media_id:
            clauses.append("media_id=?")
            params.append(media_id)
        if url:
            clauses.append("url=?")
            params.append(url)
        if not clauses:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT request_id, title, url, output_path, status, created_at, media_id, uploader, duration, "
                "media_type, output_format, quality, playlist_id, playlist_title, playlist_index, file_size, file_state "
                f"FROM history WHERE status=? AND ({' OR '.join(clauses)}) ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return HistoryEntry(**dict(row)) if row else None

    def archived_media_ids(self, media_ids: list[str] | None = None, playlist_id: str = "") -> set[str]:
        clauses = ["media_id<>''"]
        params: list[object] = []
        if media_ids:
            placeholders = ",".join("?" for _item in media_ids)
            clauses.append(f"media_id IN ({placeholders})")
            params.extend(media_ids)
        if playlist_id:
            clauses.append("playlist_id=?")
            params.append(playlist_id)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT media_id FROM download_archive WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
        return {str(row["media_id"]) for row in rows}

    def archive_playlist_items(self, request: DownloadRequest, output_path: str, completed_at: str) -> None:
        if not request.playlist_media_ids:
            return
        with self.connect() as db:
            for media_id in request.playlist_media_ids:
                if not media_id:
                    continue
                url = f"https://www.youtube.com/watch?v={media_id}"
                db.execute(
                    "INSERT INTO download_archive(media_key, media_id, url, title, uploader, output_path, output_format, "
                    "playlist_id, playlist_title, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(media_key) DO UPDATE SET playlist_id=excluded.playlist_id, "
                    "playlist_title=excluded.playlist_title, completed_at=excluded.completed_at, last_seen_at=CURRENT_TIMESTAMP",
                    (
                        media_id,
                        media_id,
                        url,
                        request.title,
                        request.uploader,
                        output_path,
                        request.output_format,
                        request.playlist_id or request.media_id,
                        request.playlist_title or request.title,
                        completed_at,
                    ),
                )

    def reconcile_library_files(self, limit: int = 1000) -> dict[str, int]:
        present = missing = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, output_path FROM history WHERE status=? AND output_path<>'' ORDER BY id DESC LIMIT ?",
                (DownloadStatus.COMPLETED.value, max(1, min(5000, limit))),
            ).fetchall()
            for row in rows:
                path = Path(str(row["output_path"]))
                exists = path.is_file()
                size = path.stat().st_size if exists else 0
                state = "present" if exists else "missing"
                db.execute("UPDATE history SET file_state=?, file_size=? WHERE id=?", (state, size, row["id"]))
                present += int(exists)
                missing += int(not exists)
        return {"present": present, "missing": missing}

    def relocate_history(self, request_id: str, output_path: str) -> bool:
        path = Path(output_path)
        if not path.is_file():
            return False
        with self.connect() as db:
            result = db.execute(
                "UPDATE history SET output_path=?, file_state='present', file_size=? WHERE request_id=?",
                (str(path), path.stat().st_size, request_id),
            )
            db.execute(
                "UPDATE download_archive SET output_path=?, last_seen_at=CURRENT_TIMESTAMP "
                "WHERE media_id=(SELECT media_id FROM history WHERE request_id=? ORDER BY id DESC LIMIT 1)",
                (str(path), request_id),
            )
        return bool(result.rowcount)

    def duplicate_library_groups(self) -> list[list[HistoryEntry]]:
        with self.connect() as db:
            media_rows = db.execute(
                "SELECT media_id FROM history WHERE status=? AND media_id<>'' GROUP BY media_id HAVING COUNT(*)>1",
                (DownloadStatus.COMPLETED.value,),
            ).fetchall()
        groups: list[list[HistoryEntry]] = []
        for media_row in media_rows:
            with self.connect() as db:
                rows = db.execute(
                    "SELECT request_id, title, url, output_path, status, created_at, media_id, uploader, duration, "
                    "media_type, output_format, quality, playlist_id, playlist_title, playlist_index, file_size, file_state "
                    "FROM history WHERE status=? AND media_id=? ORDER BY id DESC",
                    (DownloadStatus.COMPLETED.value, media_row["media_id"]),
                ).fetchall()
            groups.append([HistoryEntry(**dict(row)) for row in rows])
        return groups

    def clear_history(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM history")

    def history_summary(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS total FROM history GROUP BY status").fetchall()
        result = {row["status"]: int(row["total"]) for row in rows}
        result["total"] = sum(result.values())
        return result
