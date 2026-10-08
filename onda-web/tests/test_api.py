import io
import math
import struct
import tempfile
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from yt_dlp.networking import Response

from api import engine, index, security
from api.security import AudioError, BoundedResponse


@pytest.fixture
def media_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        samples = (int(10000 * math.sin(2 * math.pi * 440 * n / 44100)) for n in range(44100))
        audio.writeframes(b"".join(struct.pack("<hh", sample, sample) for sample in samples))
    return data.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path, media_bytes):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])

    class FakePublicTransport:
        def __init__(self, guard): self.guard = guard
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, request):
            data = b"" if request.method == "HEAD" else media_bytes
            response = Response(io.BytesIO(data), request.url,
                                {"Content-Type": "audio/wav", "Content-Length": str(len(media_bytes))})
            return BoundedResponse(response, self.guard)

    monkeypatch.setattr(engine, "direct_handler", FakePublicTransport)
    directories = []
    original_mkdtemp = tempfile.mkdtemp
    def create_temp(**kwargs):
        kwargs["dir"] = str(tmp_path)
        directory = original_mkdtemp(**kwargs)
        directories.append(Path(directory))
        return directory
    monkeypatch.setattr(index.tempfile, "mkdtemp", create_temp)
    with TestClient(index.app) as test_client:
        test_client.created_directories = directories
        yield test_client
    assert all(not path.exists() for path in directories)


def test_health_and_inspect(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["maxDuration"] is None
    assert health.json()["durationLimited"] is False
    assert health.json()["operationTimeoutSeconds"] == 240
    response = client.post("/api/inspect", json={"url": "https://sample.example.com/Meu%20%C3%A1udio.wav"})
    assert response.status_code == 200
    assert response.json()["title"] == "Meu áudio"
    assert response.json()["duration"] is None
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("audio_format,signature", [
    ("mp3", b"ID3"), ("m4a", b"ftyp"), ("wav", b"RIFF"), ("flac", b"fLaC"),
    ("ogg", b"OggS"), ("opus", b"OggS"), ("aac", b"\xff"), ("aiff", b"FORM"),
])
def test_real_ffmpeg_conversion_and_stream_cleanup(client, audio_format, signature):
    response = client.post("/api/download", json={"url": "https://sample.example.com/Meu%20%C3%A1udio.wav",
                "format": audio_format, "quality": "source" if audio_format in engine.LOSSLESS else "192"})
    assert response.status_code == 200, response.text
    assert signature in response.content[:12]
    assert int(response.headers["content-length"]) == len(response.content)
    assert "filename*=UTF-8''Meu%20%C3%A1udio" in response.headers["content-disposition"]
    assert response.headers["x-audio-title"] == "Meu%20%C3%A1udio"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert all(not path.exists() for path in client.created_directories)


@pytest.mark.parametrize("payload", [
    {"url": "https://example.com/file.mp3", "format": "exe"},
    {"url": "https://example.com/file.mp3", "quality": 999},
    {"url": "https://example.com/file.mp3", "format": "mp3", "quality": "source"},
])
def test_rejects_unsupported_format_and_quality(client, payload):
    response = client.post("/api/download", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_rejects_internal_url_and_unsupported_page(client):
    assert client.post("/api/inspect", json={"url": "http://127.0.0.1/audio.mp3"}).status_code == 400
    response = client.post("/api/inspect", json={"url": "https://example.com/article"})
    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_source"


def test_failed_conversion_cleans_directory_and_releases_slot(client, monkeypatch):
    def fail(*_args):
        raise AudioError("Sem faixa de áudio.", "conversion_failed", 422)
    monkeypatch.setattr(engine, "convert_audio", fail)
    response = client.post("/api/download", json={"url": "https://example.com/audio.wav"})
    assert response.status_code == 422
    assert response.json()["code"] == "conversion_failed"
    assert all(not path.exists() for path in client.created_directories)
    assert index.SLOTS.acquire(blocking=False)
    assert index.SLOTS.acquire(blocking=False)
    index.SLOTS.release()
    index.SLOTS.release()


def test_storage_failure_releases_slot(client, monkeypatch):
    monkeypatch.setattr(index.tempfile, "mkdtemp", lambda **_kwargs: (_ for _ in ()).throw(OSError("no space")))
    response = client.post("/api/download", json={"url": "https://example.com/audio.wav"})
    assert response.status_code == 503
    assert response.json()["code"] == "storage_busy"
    assert index.SLOTS.acquire(blocking=False)
    assert index.SLOTS.acquire(blocking=False)
    index.SLOTS.release()
    index.SLOTS.release()


def test_busy_returns_retryable_error(client):
    index.SLOTS.acquire()
    index.SLOTS.acquire()
    try:
        response = client.post("/api/download", json={"url": "https://example.com/audio.wav"})
        assert response.status_code == 429
        assert response.json()["code"] == "busy"
    finally:
        index.SLOTS.release()
        index.SLOTS.release()


def test_lossless_audio_conversion_has_no_implicit_truncation(tmp_path, media_bytes):
    source = tmp_path / "source.wav"
    source.write_bytes(media_bytes)
    target = engine.convert_audio(source, tmp_path, "wav", "source", security.Guard())
    with wave.open(str(source), "rb") as original, wave.open(str(target), "rb") as downloaded:
        assert downloaded.getparams() == original.getparams()
        assert downloaded.readframes(downloaded.getnframes()) == original.readframes(original.getnframes())


def test_platform_native_downloader_finishes_first_success(client, monkeypatch, media_bytes):
    # Real yt-dlp native download path with only its upstream HTTP response mocked.
    def extracted(_self, _url, download=False):
        return {"id": "fixture", "title": "Faixa de teste", "duration": 1,
            "extractor": "youtube", "extractor_key": "Youtube", "webpage_url": _url,
            "formats": [{"format_id": "audio", "url": "https://cdn.example.com/audio.wav",
                "ext": "wav", "acodec": "pcm_s16le", "vcodec": "none", "protocol": "https"}]}
    def fake_http(_self, request):
        return BoundedResponse(Response(io.BytesIO(media_bytes), request.url,
            {"Content-Type": "audio/wav", "Content-Length": str(len(media_bytes))}), _self.guard)
    monkeypatch.setattr(engine.SafeYoutubeDL, "extract_info", extracted)
    monkeypatch.setattr(security.PublicRH, "_send", fake_http)
    response = client.post("/api/download", json={"url": "https://www.youtube.com/watch?v=fixture", "format": "mp3"})
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"ID3")


def test_inspect_does_not_apply_metadata_budget_to_direct_media_size(client, monkeypatch):
    class MetadataTransport:
        def __init__(self, guard): self.guard = guard
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, request):
            return BoundedResponse(Response(io.BytesIO(b""), request.url,
                {"Content-Type": "audio/mpeg", "Content-Length": str(24 * 1024 * 1024)}), self.guard)
    monkeypatch.setattr(engine, "direct_handler", MetadataTransport)
    response = client.post("/api/inspect", json={"url": "https://example.com/audio.mp3"})
    assert response.status_code == 200


def test_cancelled_request_waits_for_worker_exit():
    import asyncio
    import threading
    import time
    completed = threading.Event()
    started = threading.Event()
    guard = security.Guard()

    class FakeRequest:
        async def is_disconnected(self): return False

    def worker(job_guard):
        started.set()
        while not job_guard.cancelled.is_set():
            time.sleep(.005)
        # Simulate a network/FFmpeg worker taking a moment to close resources.
        time.sleep(.03)
        completed.set()
        job_guard.check()

    async def scenario():
        task = asyncio.create_task(index.run_guarded(FakeRequest(), guard, worker))
        while not started.is_set():
            await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()

    asyncio.run(scenario())
