"""Recovery uses real owned media and keeps the original request boundaries."""
from pathlib import Path
import urllib.error

import pytest

from api import engine, index, security
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_VIDEO = "https://www.youtube.com/watch?v=BaW_jenozKc"


def details(url):
    return {"title": "Owned recovery canary", "duration": 2, "thumbnail": None,
            "source": "YouTube", "webpage_url": url}


def fast_retries(monkeypatch):
    monkeypatch.setattr(engine, "wait_for_retry", lambda guard, *_: guard.remaining > 8)


def test_transient_extraction_recovers_real_audio_without_resetting_bytes(monkeypatch, tmp_path):
    fast_retries(monkeypatch)
    calls = []
    payload = (ROOT / "public/canary.wav").read_bytes()

    def acquire(url, folder, guard, *_args, **_kwargs):
        calls.append(guard)
        if len(calls) == 1:
            guard.count(20)
            (folder / "failed.part").write_bytes(b"partial")
            raise RuntimeError("HTTP Error 503: Service Unavailable")
        guard.count(len(payload))
        path = folder / "source.wav"
        path.write_bytes(payload)
        return path, details(url)

    monkeypatch.setattr(engine, "acquire_media", acquire)
    guard = security.Guard()
    result, info = engine.prepare_download(PUBLIC_VIDEO, tmp_path, "mp3", 192, guard)
    assert engine.probe_media(result, guard)["audio"]
    assert len(calls) == 2 and guard.received == len(payload) + 20
    assert info["recovery"]["attempts"] == 2
    assert not (tmp_path / "attempt-1").exists()


def test_expired_stream_refresh_then_alternative_recovers_real_video(monkeypatch, tmp_path):
    fast_retries(monkeypatch)
    calls = []
    payload = (ROOT / "public/canary.mp4").read_bytes()

    def acquire(url, folder, guard, *_args, **_kwargs):
        calls.append((guard.profile, guard.excluded_formats))
        if len(calls) <= 2:
            raw = urllib.error.HTTPError("https://cdn.example.com/expired.mp4", 403, "Forbidden", {}, None)
            error = security.AudioError("A plataforma bloqueou o acesso ou exige login.", "platform_blocked", 422)
            error.recovery_phase = "transfer"
            error.recovery_format_id = "expired-4k"
            raise error from raw
        guard.count(len(payload))
        path = folder / "source.mp4"
        path.write_bytes(payload)
        return engine.VideoSources(path), details(url)

    monkeypatch.setattr(engine, "acquire_video_media", acquire)
    guard = security.Guard()
    result, info = engine.prepare_download(PUBLIC_VIDEO, tmp_path, "mp4", "source", guard,
                                          settings=engine.MediaSettings(media_type="video"))
    inspected = engine.probe_media(result, guard)
    assert inspected["video"] and inspected["audio"]
    assert calls == [("default", frozenset()), ("default", frozenset()),
                     ("youtube_hls", frozenset({"expired-4k"}))]
    assert info["recovery"]["attempts"] == 3
    assert info["output_resolution"] == min(inspected["width"], inspected["height"])
    assert guard.received == len(payload)


def test_explicit_login_block_stops_without_retry_or_alternate_client(monkeypatch, tmp_path):
    calls = []

    def acquire(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(engine, "acquire_media", acquire)
    with pytest.raises(security.AudioError) as failed:
        engine.prepare_download(PUBLIC_VIDEO, tmp_path, "mp3", 192, security.Guard())
    assert failed.value.code == "platform_blocked"
    assert len(calls) == 1 and not (tmp_path / "attempt-1").exists()


def test_failed_copy_can_reencode_same_source_without_downloading_it_again(monkeypatch, tmp_path):
    acquisitions, encodings = [], []
    original_convert = engine.convert_video

    def acquire(url, folder, guard, *_args, **_kwargs):
        acquisitions.append(1)
        path = folder / "source.mp4"
        path.write_bytes((ROOT / "public/canary.mp4").read_bytes())
        return path, details(url)

    def convert(source, directory, format, quality, guard, settings):
        encodings.append(getattr(guard, "force_encode", False))
        if len(encodings) == 1:
            guard.copy_attempted = True
            raise security.AudioError("Container rejected the original codec", "conversion_failed", 422)
        return original_convert(source, directory, format, quality, guard, settings)

    monkeypatch.setattr(engine, "acquire_video_media", acquire)
    monkeypatch.setattr(engine, "convert_video", convert)
    result, info = engine.prepare_download(PUBLIC_VIDEO, tmp_path, "mp4", "source", security.Guard(),
                                          settings=engine.MediaSettings(media_type="video"))
    assert engine.probe_media(result, security.Guard())["video"]
    assert acquisitions == [1] and encodings == [False, True]
    assert info["recovery"]["conversion_recovered"] is True


def test_api_reports_recovery_and_actual_resolution_for_a_completed_file(monkeypatch):
    fast_retries(monkeypatch)
    calls = []

    def acquire(url, folder, guard, *_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError("Connection reset by peer")
        path = folder / "source.mp4"
        path.write_bytes((ROOT / "public/canary.mp4").read_bytes())
        return path, details(url)

    monkeypatch.setattr(engine, "acquire_video_media", acquire)
    with TestClient(index.app) as client:
        response = client.post("/api/download", json={"url": PUBLIC_VIDEO, "media_type": "video", "format": "mp4"})
    assert response.status_code == 200
    assert response.headers["X-Recovery-Attempts"] == "2"
    assert response.headers["X-Recovery-Resumed"] == "0"
    assert response.headers["X-Media-Resolution"] == "180"
    assert len(response.content) > 1000


def test_temporary_worker_contention_waits_before_processing_instead_of_failing(monkeypatch):
    original_acquire = index.acquire_slot
    calls = []

    def temporarily_busy():
        calls.append(1)
        if len(calls) == 1:
            raise security.AudioError("Busy", "busy", 429)
        original_acquire()

    def acquire(url, folder, guard, *_args, **_kwargs):
        path = folder / "source.wav"
        path.write_bytes((ROOT / "public/canary.wav").read_bytes())
        return path, details(url)

    monkeypatch.setattr(index, "acquire_slot", temporarily_busy)
    monkeypatch.setattr(engine, "acquire_media", acquire)
    with TestClient(index.app) as client:
        response = client.post("/api/download", json={"url": PUBLIC_VIDEO, "format": "mp3"})
    assert response.status_code == 200
    assert response.headers["X-Recovery-Queued"] == "1"
    assert len(calls) == 2
    # Streaming cleanup released the real slot that was eventually acquired.
    original_acquire()
    index.SLOTS.release()
