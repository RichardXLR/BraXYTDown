"""The opt-in wire format carries real counters and fails closed on teardown."""
import asyncio
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError

from api import engine, index, security
from api.progress import CONTENT_TYPE, ProgressReporter, accepts_stream, frame, binary_frame
from api.security import AudioError, Guard, BoundedResponse


def decode(data):
    events, chunks = [], []
    offset = 0
    while offset < len(data):
        kind, size = data[offset], struct.unpack(">I", data[offset + 1:offset + 5])[0]
        payload = data[offset + 5:offset + 5 + size]
        assert len(payload) == size
        (events if kind == 1 else chunks).append(json.loads(payload) if kind == 1 else payload)
        offset += 5 + size
    return events, b"".join(chunks)


@pytest.mark.parametrize("value,expected", [(CONTENT_TYPE, True), ("application/x-onda-download; version=1", True),
    ("audio/mpeg", False), ("application/x-onda-download;version=2", False),
    ("application/x-onda-download;version=1;q=0", False), ("*/*", False)])
def test_progress_requires_explicit_versioned_opt_in(value, expected):
    assert accepts_stream(value) is expected


def test_opt_in_frames_and_legacy_bytes_share_one_prepare_and_cleanup(monkeypatch, tmp_path):
    calls, paths = [], []
    original = tempfile.mkdtemp
    def mkdtemp(**kwargs):
        kwargs["dir"] = str(tmp_path)
        path = Path(original(**kwargs))
        paths.append(path)
        return str(path)
    data = b"owned recording" * 12000
    def prepare(url, directory, format, quality, guard, **kwargs):
        calls.append(guard)
        guard.report("downloading", downloadedBytes=1024, totalBytes=4096, speedBytesPerSecond=256)
        time.sleep(.015)
        guard.report("converting", processedSeconds=2.5, durationSeconds=4, outputBytes=8192)
        target = directory / "audio.mp3"
        target.write_bytes(data)
        return target, {"title": "Owned recording", "recovery": {"attempts": 2, "resumed": True}}
    monkeypatch.setattr(index.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(index, "prepare_download", prepare)
    with TestClient(index.app) as client:
        stream = client.post("/api/download", headers={"Accept": CONTENT_TYPE}, json={"url": "https://media.example.com/owned.mp3"})
        assert stream.status_code == 200
        assert stream.headers["content-type"] == CONTENT_TYPE
        assert "content-length" not in stream.headers
        events, payload = decode(stream.content)
        assert payload == data
        assert events[0] == {"type": "progress", "stage": "extracting"}
        assert [event["stage"] for event in events if event.get("type") == "progress"][:3] == ["extracting", "downloading", "converting"]
        file = next(event for event in events if event["type"] == "file")
        assert file["size"] == len(data) and file["mime"] == "audio/mpeg"
        assert file["recovery"] == {"attempts": 2, "resumed": True, "conversion_recovered": False, "queued": False}
        assert events[-1] == {"type": "complete", "size": len(data)}
        legacy = client.post("/api/download", json={"url": "https://media.example.com/owned.mp3"})
        assert legacy.content == data and int(legacy.headers["content-length"]) == len(data)
    assert len(calls) == 2 and all(not path.exists() for path in paths)


@pytest.mark.parametrize("failure", [AudioError("A origem demorou demais.", "timeout", 504), RuntimeError("https://secret.example/?token=private")])
def test_processing_failure_has_no_complete_file_or_upstream_secret(monkeypatch, failure):
    def prepare(*args, **kwargs):
        raise failure
    monkeypatch.setattr(index, "prepare_download", prepare)
    with TestClient(index.app) as client:
        response = client.post("/api/download", headers={"Accept": CONTENT_TYPE}, json={"url": "https://media.example.com/owned.mp3"})
    events, payload = decode(response.content)
    assert events[-1]["type"] == "error" and not payload
    assert not any(event["type"] in ("file", "complete") for event in events)
    assert b"secret.example" not in response.content and b"token=" not in response.content


def test_progress_reporter_is_bounded_throttled_and_never_exports_track_names():
    clock = [0.0]
    reporter = ProgressReporter(clock=lambda: clock[0])
    reporter.begin_download({"/tmp/private/cookies-secret": 1000, "signed-stream-url": 500})
    for n in range(1, 100):
        reporter.transfer("/tmp/private/cookies-secret", n * 10, 1000)
        clock[0] += .01
    reporter.transfer("signed-stream-url", 500, 500, finished=True)
    events = []
    while not reporter.events.empty():
        events.append(reporter.events.get_nowait())
    assert len(events) <= 8
    assert len(events) <= 6
    assert events[-1]["downloadedBytes"] == 1490 and events[-1]["totalBytes"] == 1500
    assert any(event.get("speedBytesPerSecond", 0) > 0 for event in events)
    assert "cookies-secret" not in str(events) and "signed-stream" not in str(events)
    for n in range(100):
        reporter.emit("converting", force=True, processedSeconds=n, secret="token", totalBytes=float("nan"))
    assert reporter.events.qsize() == 8
    assert all("secret" not in event and "totalBytes" not in event for event in list(reporter.events.queue))


def test_resume_speed_counts_new_network_bytes_not_the_saved_prefix():
    clock = [0.0]
    reporter = ProgressReporter(clock=lambda: clock[0])
    reporter.begin_download({"direct": 10000}, offsets={"direct": 9000}, attempt=2)
    clock[0] = 1.0
    reporter.transfer("direct", 10000, 10000, finished=True, attempt=2)
    events = list(reporter.events.queue)
    assert events[0]["downloadedBytes"] == 9000
    assert events[-1]["downloadedBytes"] == 10000
    assert events[-1]["speedBytesPerSecond"] == 1000


def test_real_direct_transfer_and_ffmpeg_expose_bytes_and_media_time(monkeypatch, tmp_path):
    data = (Path(__file__).parents[1] / "public" / "canary.wav").read_bytes()
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *args, **kwargs:
        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    class Handler:
        def __init__(self, guard): self.guard = guard
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send(self, request):
            return BoundedResponse(Response(io.BytesIO(data), request.url, {"Content-Type": "audio/wav", "Content-Length": str(len(data))}), self.guard)
    monkeypatch.setattr(engine, "direct_handler", Handler)
    reporter = ProgressReporter()
    guard = Guard(progress=reporter)
    target, _ = engine.prepare_download("https://media.example.com/owned.wav", tmp_path, "mp3", 192, guard)
    assert target.stat().st_size > 0
    events = list(reporter.events.queue)
    transfers = [event for event in events if event["stage"] == "downloading"]
    assert transfers[-1]["downloadedBytes"] == len(data) == transfers[-1]["totalBytes"]
    conversions = [event for event in events if event["stage"] == "converting"]
    assert any(event.get("processedSeconds", 0) > 0 for event in conversions)
    assert any(event.get("durationSeconds", 0) > 0 for event in conversions)
    assert any(event.get("outputBytes", 0) > 0 for event in conversions)


def test_native_hooks_report_payload_counters_without_metadata_budget_or_filename():
    reporter = ProgressReporter()
    guard = Guard(progress=reporter)
    guard.count(2048)  # This can be a webpage: it must not become transfer progress.
    hook = engine.options(guard)["progress_hooks"][0]
    hook({"status": "finished", "filename": "/tmp/secret-source", "downloaded_bytes": 123, "total_bytes": 123})
    event = reporter.events.get_nowait()
    assert event["downloadedBytes"] == event["totalBytes"] == 123
    assert "secret-source" not in str(event)


def test_extraction_frame_arrives_while_worker_is_still_processing(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    def prepare(url, directory, format, quality, guard, **kwargs):
        assert release.wait(2)
        target = directory / "audio.mp3"
        target.write_bytes(b"owned")
        finished.set()
        return target, {"title": "Owned"}
    monkeypatch.setattr(index, "prepare_download", prepare)
    class Request:
        headers = {"accept": CONTENT_TYPE}
        async def is_disconnected(self): return False
    delivered = []
    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            events, payload = decode(message["body"])
            if events and events[0].get("stage") == "extracting":
                assert not finished.is_set()
                release.set()
            delivered.extend(events)
    async def receive(): await asyncio.sleep(60)
    async def scenario():
        response = await index.download_link(index.DownloadInput(url="https://media.example.com/owned.mp3"), Request())
        await response({"type": "http", "method": "POST", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert delivered[-1] == {"type": "complete", "size": 5}
    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_stream_asgi_failure_cleans_and_never_abandons_worker(monkeypatch, tmp_path, stage):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(index, "SLOTS", slots)
    paths, started, completed = [], threading.Event(), threading.Event()
    original = tempfile.mkdtemp
    def mkdtemp(**kwargs):
        kwargs["dir"] = str(tmp_path)
        path = Path(original(**kwargs)); paths.append(path)
        return str(path)
    monkeypatch.setattr(index.tempfile, "mkdtemp", mkdtemp)
    def prepare(url, directory, format, quality, guard, **kwargs):
        started.set()
        guard.cancelled.wait(2)
        time.sleep(.02)
        completed.set()
        guard.check()
    monkeypatch.setattr(index, "prepare_download", prepare)
    class Request:
        headers = {"accept": CONTENT_TYPE}
        async def is_disconnected(self): return False
    async def receive(): await asyncio.sleep(60)
    async def send(message):
        if stage == "headers" and message["type"] == "http.response.start":
            raise OSError("Closed client")
        if stage == "body" and message["type"] == "http.response.body":
            assert await asyncio.to_thread(started.wait, 1)
            raise OSError("Closed client")
    async def scenario():
        response = await index.download_link(index.DownloadInput(url="https://media.example.com/owned.mp3"), Request())
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "method": "POST", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert completed.is_set() if stage == "body" else not started.is_set()
        assert all(not path.exists() for path in paths)
        assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
        slots.release(); slots.release()
    asyncio.run(scenario())


def test_inspection_sanitizes_formats_and_preserves_real_quality_values():
    result = engine.metadata({"title": "Owned", "duration": 120, "filesize": 8000, "tbr": 512,
        "vcodec": "avc1.42", "acodec": "mp4a.40.2", "height": 2160, "width": 3840,
        "formats": [{"url": "https://signed.example/?token=secret", "http_headers": {"Cookie": "secret"},
                     "filesize": 12345, "height": 1440, "width": 2560, "tbr": 2000, "abr": 128,
                     "vbr": 1872, "vcodec": "vp9", "acodec": "opus", "ext": "webm"},
                    {"filesize": float("inf"), "width": True, "height": -2, "tbr": "secret", "vcodec": "h264"}]},
        "https://vimeo.com/123", "Vimeo")
    assert result["sourceSizeBytes"] == 8000 and result["sourceBitrateKbps"] == 512
    assert result["videoCodec"] == "h264" and result["audioCodec"] == "aac"
    format = result["formats"][0]
    assert format["resolution"] == 1440 and format["sizeBytes"] == 12345
    assert format["videoBitrateKbps"] == 1872 and format["audioBitrateKbps"] == 128
    assert "secret" not in str(result) and "signed.example" not in str(result)
    assert all("url" not in format and "http_headers" not in format for format in result["formats"])


def test_large_format_catalog_keeps_uhd_and_audio_while_remaining_bounded():
    formats = [{"vcodec": "h264", "acodec": "none", "height": 2160, "width": 3840, "tbr": 1000 + n}
               for n in range(80)]
    formats.extend([{"vcodec": "none", "acodec": "opus", "abr": 128 + n, "tbr": 128 + n}
                    for n in range(10)])
    result = engine.metadata({"duration": True, "formats": formats}, "https://vimeo.com/123", "Vimeo")
    assert result["duration"] is None
    assert len(result["formats"]) == 64
    assert result["formats"][0]["resolution"] == 2160
    assert sum(not item["hasVideo"] and item["hasAudio"] for item in result["formats"]) == 4


@pytest.mark.parametrize("headers,expected,status", [
    ({"Content-Length": "1", "Content-Range": "bytes 0-0/123456"}, 123456, 206),
    ({"Content-Length": "500"}, 500, 200),
    ({"Content-Length": "1", "Content-Range": "bytes 1-1/123456"}, None, 206),
    ({"Content-Length": "2", "Content-Range": "bytes 0-0/123456"}, None, 206),
    ({"Content-Length": "9" * 5000}, None, 200),
    ({"Content-Length": "1", "Content-Range": "bytes 0-0/999999999"}, None, 206),
])
def test_head_fallback_inspection_uses_full_range_size_and_rejects_invalid_sizes(monkeypatch, headers, expected, status):
    calls, closed = [], []
    monkeypatch.setattr(engine, "public_url", lambda *args, **kwargs: ("media.example.com", []))
    class Handler:
        def __init__(self, guard): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send(self, request):
            calls.append(request)
            if request.method == "HEAD":
                response = Response(io.BytesIO(b""), request.url, {}, 405)
                original = response.close
                def close(): closed.append(True); original()
                response.close = close
                raise HTTPError(response)
            return Response(io.BytesIO(b""), request.url, {"Content-Type": "video/mp4", **headers}, status)
    monkeypatch.setattr(engine, "direct_handler", Handler)
    if expected is None:
        with pytest.raises(AudioError):
            engine.inspect_media("https://media.example.com/owned.mp4", Guard())
    else:
        result = engine.inspect_media("https://media.example.com/owned.mp4", Guard())
        assert result["sourceSizeBytes"] == expected and result["duration"] is None
    assert len(calls) == 2 and calls[1].headers["Range"] == "bytes=0-0" and closed
