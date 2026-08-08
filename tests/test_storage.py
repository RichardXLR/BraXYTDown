from pathlib import Path

import json

from baixatube.models import DownloadRequest, DownloadStatus, HistoryEntry
from baixatube.storage import Storage


def test_settings_queue_and_history(tmp_path: Path):
    storage = Storage(tmp_path / "test.db")
    storage.save_settings({"concurrency": 4})
    assert storage.settings()["concurrency"] == 4

    request = DownloadRequest("id", "https://youtu.be/x", "Título", str(tmp_path))
    storage.save_queue_item(request, DownloadStatus.DOWNLOADING)
    recovered = storage.recoverable_queue()
    assert recovered == [request]
    storage.save_queue_item(request, DownloadStatus.COMPLETED)
    assert storage.recoverable_queue() == []

    entry = HistoryEntry("id", "Título", request.url, "file.mp4", "concluído", "2026-01-01")
    storage.add_history(entry)
    assert storage.history()[0] == entry


def test_corrupt_queue_row_is_quarantined_without_blocking_valid_items(tmp_path: Path):
    storage = Storage(tmp_path / "test.db")
    valid = DownloadRequest("valid", "https://youtu.be/x", "Título", str(tmp_path))
    storage.save_queue_item(valid, DownloadStatus.WAITING)
    with storage.connect() as db:
        db.execute(
            "INSERT INTO queue(request_id, payload, status) VALUES (?, ?, ?)",
            ("broken", "{not-json", DownloadStatus.WAITING.value),
        )

    assert storage.recoverable_queue() == [valid]
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM queue WHERE request_id='broken'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM queue_errors WHERE request_id='broken'").fetchone()[0] == 1


def test_damaged_setting_is_ignored_without_losing_defaults(tmp_path: Path):
    storage = Storage(tmp_path / "settings.db")
    with storage.connect() as db:
        db.execute("INSERT INTO settings(key, value) VALUES (?, ?)", ("concurrency", "{broken"))
        db.execute("INSERT INTO settings(key, value) VALUES (?, ?)", ("quality", json.dumps("720")))
    settings = storage.settings()
    assert settings["concurrency"] == 2
    assert settings["quality"] == "720"
    assert settings["auto_update_tools"] is True
    assert settings["cookie_mode"] == "off"
    assert settings["cookie_consent"] is False


def test_queue_payload_is_backward_and_forward_compatible(tmp_path: Path):
    storage = Storage(tmp_path / "queue.db")
    old_payload = {
        "id": "old",
        "url": "https://youtu.be/old",
        "title": "Antigo",
        "destination": str(tmp_path),
        "future_field": "ignored",
    }
    with storage.connect() as db:
        db.execute(
            "INSERT INTO queue(request_id, payload, status) VALUES (?, ?, ?)",
            ("old", json.dumps(old_payload), DownloadStatus.DOWNLOADING.value),
        )
    recovered = storage.recoverable_queue()
    assert len(recovered) == 1
    assert recovered[0].id == "old"
    assert recovered[0].retries == 10
    assert not hasattr(recovered[0], "future_field")


def test_history_search_status_summary_and_clear(tmp_path: Path):
    storage = Storage(tmp_path / "history.db")
    entries = [
        HistoryEntry("1", "A_100%", "https://youtu.be/1", "a.mp4", DownloadStatus.COMPLETED.value, "2026-01-01"),
        HistoryEntry("2", "Outro", "https://youtu.be/2", "", DownloadStatus.ERROR.value, "2026-01-02"),
    ]
    for entry in entries:
        storage.add_history(entry)

    assert storage.history(search="A_100%") == [entries[0]]
    assert storage.history(status=DownloadStatus.ERROR.value) == [entries[1]]
    assert storage.history_summary() == {
        DownloadStatus.COMPLETED.value: 1,
        DownloadStatus.ERROR.value: 1,
        "total": 2,
    }
    storage.clear_history()
    assert storage.history() == []
    assert storage.history_summary() == {"total": 0}


def test_completed_duplicate_uses_media_id_or_exact_url(tmp_path: Path):
    storage = Storage(tmp_path / "duplicates.db")
    entry = HistoryEntry(
        "1",
        "Vídeo",
        "https://www.youtube.com/watch?v=abc",
        "video.mp4",
        DownloadStatus.COMPLETED.value,
        "2026-01-01",
        "abc",
    )
    storage.add_history(entry)

    assert storage.completed_duplicate("abc", "") == entry
    assert storage.completed_duplicate("", entry.url) == entry
    assert storage.completed_duplicate("xyz", "https://youtu.be/xyz") is None


def test_archive_tracks_playlist_items_and_missing_files(tmp_path: Path):
    storage = Storage(tmp_path / "library.db")
    output = tmp_path / "video.mp4"
    output.write_bytes(b"media")
    request = DownloadRequest(
        "playlist",
        "https://youtube.com/playlist?list=PL1",
        "Coleção",
        str(tmp_path),
        media_id="PL1",
        playlist_id="PL1",
        playlist_title="Coleção",
        playlist_media_ids=["video-a", "video-b"],
    )
    storage.archive_playlist_items(request, str(output), "2026-08-07T12:00:00+00:00")

    assert storage.archived_media_ids(["video-a", "video-c"], "PL1") == {"video-a"}

    storage.add_history(
        HistoryEntry(
            "download-a",
            "Vídeo",
            "https://youtu.be/video-a",
            str(output),
            DownloadStatus.COMPLETED.value,
            "2026-08-07T12:00:00+00:00",
            "video-a",
            output_format="mp4",
        )
    )
    assert storage.reconcile_library_files()["present"] == 1
    output.unlink()
    assert storage.reconcile_library_files()["missing"] == 1
    assert storage.history()[0].file_state == "missing"
