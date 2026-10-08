"""Exercise real native HLS workers with one bounded, request-scoped transport."""
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from yt_dlp.networking import Response
from yt_dlp.networking._urllib import UrllibRH

from api import engine, index
from api.security import AudioError, Guard


SOURCE = Path(__file__).resolve().parents[1] / "public" / "canary.ts"


def hls_origin(monkeypatch, guard, *, delay=.04, block=False):
    """Model public upstream latency without using any external service."""
    original = SOURCE.read_bytes()
    packet_count = len(original) // 188
    split = [packet_count * n // 4 * 188 for n in range(5)]
    fragments = [original[split[n]:split[n + 1]] for n in range(4)]
    # Include any trailing bytes as well; the reconstructed file must match exactly.
    fragments[-1] += original[split[-1]:]
    manifest = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
    manifest += "".join(f"#EXTINF:0.5,\nhttps://cdn.example.com/segment-{n}.ts\n" for n in range(4))
    manifest += "#EXT-X-ENDLIST\n"
    state = {"active": 0, "peak": 0, "requests": [], "sockets": []}
    lock = threading.Lock()
    ready = threading.Event()

    class FakeSocket:
        def __init__(self):
            self.closed = threading.Event()

        def shutdown(self, _how):
            self.closed.set()

        def close(self):
            self.closed.set()

    class Segment(io.BytesIO):
        def __init__(self, data, connection):
            super().__init__(data)
            self.connection = connection
            self.first = True

        def read(self, amount=-1):
            if self.first:
                self.first = False
                if block:
                    assert self.connection.closed.wait(3), "Cancellation failed to interrupt a fragment"
                else:
                    time.sleep(delay)
            return super().read(amount)

        def close(self):
            if not self.closed:
                with lock:
                    state["active"] -= 1
            super().close()

    def upstream(_handler, request):
        index = int(request.url.rsplit("segment-", 1)[1].split(".", 1)[0])
        connection = FakeSocket()
        guard.register_socket(connection)
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["requests"].append(request.url)
            state["sockets"].append(connection)
            if state["active"] == 4:
                ready.set()
        data = fragments[index]
        return Response(Segment(data, connection), request.url,
                        {"Content-Length": str(len(data)), "Content-Type": "video/mp2t"})

    monkeypatch.setattr(UrllibRH, "_send", upstream)
    info = {"id": "owned-canary", "title": "Owned test fixture", "ext": "ts",
            "url": "https://cdn.example.com/manifest.m3u8", "protocol": "m3u8_native",
            "hls_media_playlist_data": manifest}
    return original, info, state, ready


def native_hls(directory, guard, info, concurrency=None):
    opts = engine.options(guard, directory)
    if concurrency is not None:
        opts["concurrent_fragment_downloads"] = concurrency
    with engine.SafeYoutubeDL(opts, guard) as downloader:
        return downloader.dl(str(directory / "received.ts"), info)


def test_missing_hls_fragment_is_a_failed_download_instead_of_an_incomplete_success(monkeypatch, tmp_path):
    from yt_dlp.networking.exceptions import HTTPError
    guard = Guard()
    _original, info, _state, _ready = hls_origin(monkeypatch, guard, delay=.002)
    original_send = UrllibRH._send

    def missing_fragment(handler, request):
        if request.url.endswith("segment-1.ts"):
            raise HTTPError(Response(io.BytesIO(b"Not found"), request.url, {}, status=404))
        return original_send(handler, request)

    monkeypatch.setattr(UrllibRH, "_send", missing_fragment)
    with pytest.raises(AudioError):
        native_hls(tmp_path, guard, info)
    assert not (tmp_path / "received.ts").exists()
    guard.close_sockets()


def test_interrupted_hls_fragment_restarts_without_appending_a_changed_representation(monkeypatch, tmp_path):
    guard = Guard()
    original, info, _state, _ready = hls_origin(monkeypatch, guard, delay=.002)
    original_send = UrllibRH._send
    split = [(len(original) // 188) * n // 4 * 188 for n in range(5)]
    segment = original[split[1]:split[2]]
    prefix = 188 * 3
    requests = []

    class Interrupted(io.BytesIO):
        def read(self, amount=-1):
            chunk = super().read(amount)
            if not chunk:
                raise ConnectionResetError("Connection reset by peer")
            return chunk

    def changing_fragment(handler, request):
        if not request.url.endswith("segment-1.ts"):
            return original_send(handler, request)
        requests.append(request)
        if len(requests) == 1:
            return Response(Interrupted(b"x" * prefix), request.url,
                            {"Content-Length": str(len(segment)), "Content-Type": "video/mp2t"})
        if "Range" in request.headers:
            start = int(request.headers["Range"].split("=")[1].split("-")[0])
            return Response(io.BytesIO(segment[start:]), request.url,
                            {"Content-Length": str(len(segment)-start), "Content-Type": "video/mp2t",
                             "Content-Range": f"bytes {start}-{len(segment)-1}/{len(segment)}"}, 206)
        return original_send(handler, request)

    monkeypatch.setattr(UrllibRH, "_send", changing_fragment)
    monkeypatch.setattr(engine, "wait_for_retry", lambda retry_guard, *_: retry_guard.remaining > 8)

    def acquire(_url, directory, attempt_guard, *_args, **_kwargs):
        success, _ = native_hls(directory, attempt_guard, info)
        assert success
        return directory / "received.ts", {"title": "Owned HLS canary"}

    monkeypatch.setattr(engine, "acquire_media", acquire)
    path, details = engine.acquire_with_recovery("https://www.youtube.com/watch?v=BaW_jenozKc", tmp_path,
                                               "mp3", guard, engine.MediaSettings(), None)
    assert path.read_bytes() == original and details["recovery"]["attempts"] == 2
    assert len(requests) == 2 and all("Range" not in request.headers for request in requests)
    assert guard.received == len(original) * 2 - len(segment) + prefix
    assert not (tmp_path / "attempt-1").exists()
    guard.close_sockets()


def test_parallel_native_hls_preserves_bytes_and_aggregate_budget(monkeypatch, tmp_path):
    timings = {}
    expected = None
    for concurrency in (1, 4):
        directory = tmp_path / f"workers-{concurrency}"
        directory.mkdir()
        guard = Guard(maximum_bytes=SOURCE.stat().st_size)
        expected, info, state, _ready = hls_origin(monkeypatch, guard)
        before = time.monotonic()
        success, _ = native_hls(directory, guard, info, concurrency)
        timings[str(concurrency)] = round(time.monotonic() - before, 4)
        assert success
        assert state["peak"] == concurrency
        assert len(state["requests"]) == 4
        assert guard.received == len(expected)
        assert (directory / "received.ts").read_bytes() == expected
        guard.close_sockets()
    report = {"native_hls_workers": timings, "source_bytes": len(expected),
              "byte_identical": True, "parallel_workers": 4,
              "speedup": round(timings["1"] / timings["4"], 2)}
    print("\nHLS transport benchmark: " + json.dumps(report))


def test_parallel_fragments_stop_all_sockets_at_shared_limit(monkeypatch, tmp_path):
    # Every fragment fits individually, but the combined transfer cannot fit.
    guard = Guard(maximum_bytes=SOURCE.stat().st_size // 2)
    _expected, info, state, _ready = hls_origin(monkeypatch, guard)
    with pytest.raises(AudioError) as raised:
        native_hls(tmp_path, guard, info)
    assert raised.value.code == "source_too_large"
    assert guard.received > guard.maximum_bytes
    assert guard.cancelled.is_set()
    assert all(connection.closed.is_set() for connection in state["sockets"])
    with pytest.raises(AudioError) as still_original:
        guard.count(0)
    assert still_original.value.code == "source_too_large"


def test_cancel_interrupts_all_parallel_native_fragment_reads(monkeypatch, tmp_path):
    guard = Guard()
    _expected, info, state, ready = hls_origin(monkeypatch, guard, block=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(native_hls, tmp_path, guard, info)
        try:
            assert ready.wait(3), "Native HLS failed to start all four workers"
        finally:
            guard.abort()
        with pytest.raises(AudioError) as raised:
            future.result(timeout=3)
    assert raised.value.code == "cancelled"
    assert guard.received == 0
    assert state["peak"] == 4
    assert all(connection.closed.is_set() for connection in state["sockets"])


def test_concurrent_budget_counts_every_worker_even_at_thread_switches():
    class SwitchingInt(int):
        def __add__(self, size):
            # A realistic scheduling point inside the read/modify/write sequence.
            time.sleep(.0001)
            return SwitchingInt(int(self) + size)

    guard = Guard(maximum_bytes=800)
    guard.received = SwitchingInt(0)
    barrier = threading.Barrier(4)

    def consume():
        barrier.wait()
        for _ in range(100):
            guard.count(2)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _n: consume(), range(4)))
    assert guard.received == 800
    guard.count(0)
    assert not guard.cancelled.is_set()
    with pytest.raises(AudioError) as raised:
        guard.count(1)
    assert raised.value.code == "source_too_large"


def test_parallel_request_budget_and_cancel_are_isolated():
    first, second = Guard(maximum_bytes=1), Guard(maximum_bytes=4)
    with pytest.raises(AudioError):
        first.count(2)
    second.count(4)
    second.count(0)
    assert second.received == 4
    assert second.error is None
    assert not second.cancelled.is_set()


def test_failed_parallel_hls_releases_api_slot_and_temporary_files(monkeypatch, tmp_path):
    directories = []

    def temporary_directory(**_kwargs):
        directory = tmp_path / f"api-request-{len(directories)}"
        directory.mkdir()
        directories.append(directory)
        return str(directory)

    def acquire(_url, directory, guard, *_args, **_kwargs):
        (guard.parent if hasattr(guard, "parent") else guard).maximum_bytes = SOURCE.stat().st_size // 2
        _expected, info, _state, _ready = hls_origin(monkeypatch, guard, delay=.005)
        native_hls(directory, guard, info)
        pytest.fail("The combined native fragments must exceed the transfer budget")

    monkeypatch.setattr(index.tempfile, "mkdtemp", temporary_directory)
    monkeypatch.setattr(engine, "acquire_video_media", acquire)
    with TestClient(index.app) as client:
        # Three failures must not occupy the application's two conversion slots.
        for _ in range(3):
            response = client.post("/api/download", json={
                "url": "https://www.youtube.com/watch?v=owned-canary",
                "media_type": "video", "format": "mp4",
            })
            assert response.status_code == 413, response.text
            assert response.json()["code"] == "source_too_large"
    assert len(directories) == 3
    assert not any(directory.exists() for directory in directories)
