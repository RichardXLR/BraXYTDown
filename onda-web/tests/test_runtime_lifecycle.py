"""Download resources remain bounded through ASGI failures and cancellation."""
import asyncio
from pathlib import Path
import tempfile
import threading
import time

import anyio
import pytest
from starlette.requests import ClientDisconnect

from api import index
from api.security import AudioError, Guard


class ConnectedRequest:
    async def is_disconnected(self):
        return False


def test_repeated_cancellation_never_finishes_before_worker_exit():
    started = threading.Event()
    stopping = threading.Event()
    finish = threading.Event()
    completed = threading.Event()
    guard = Guard()

    def worker(job_guard):
        started.set()
        while not job_guard.cancelled.wait(.005):
            pass
        stopping.set()
        finish.wait(2)
        completed.set()
        job_guard.check()

    async def scenario():
        task = asyncio.create_task(index.run_guarded(ConnectedRequest(), guard, worker))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        assert await asyncio.to_thread(stopping.wait, 1)
        task.cancel()
        await asyncio.sleep(.02)
        returned_before_worker = task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not returned_before_worker
        assert completed.is_set()

    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_middleware_cancel_scope_waits_for_worker_to_close():
    started = threading.Event()
    completed = threading.Event()
    guard = Guard()

    def worker(job_guard):
        started.set()
        job_guard.cancelled.wait(2)
        # A socket/process teardown is slower than cancelling the ASGI task.
        time.sleep(.03)
        completed.set()
        job_guard.check()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(index.run_guarded, ConnectedRequest(), guard, worker)
            assert await asyncio.to_thread(started.wait, 1)
            group.cancel_scope.cancel()
        assert guard.cancelled.is_set()
        assert completed.is_set()

    asyncio.run(scenario())


def test_runner_preserves_first_failure_when_deadline_also_expires():
    guard = Guard()
    original = AudioError("Source size already exceeded", "source_too_large", 413)

    class DeadlineRequest:
        async def is_disconnected(self):
            guard.error = original
            guard.started -= guard.seconds + 1
            return False

    def worker(job_guard):
        job_guard.cancelled.wait(2)
        job_guard.check()

    async def scenario():
        with pytest.raises(AudioError) as failure:
            await index.run_guarded(DeadlineRequest(), guard, worker)
        assert failure.value is original

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["headers", "first_chunk", "complete", "cancel_headers", "cancel_chunk"])
def test_asgi_delivery_always_closes_file_cleans_directory_and_releases_slot(monkeypatch, tmp_path, stage):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(index, "SLOTS", slots)
    directories = []
    original_mkdtemp = tempfile.mkdtemp

    def create_directory(**kwargs):
        kwargs["dir"] = str(tmp_path)
        directory = Path(original_mkdtemp(**kwargs))
        directories.append(directory)
        return str(directory)

    def prepare(_url, directory, _format, _quality, _guard, **_kwargs):
        target = directory / "owned.mp4"
        # Two chunks ensure cancellation can leave the iterator suspended
        # with an open file descriptor instead of already reaching EOF.
        target.write_bytes(b"owned media" * (128 * 1024))
        return target, {"title": "Owned media"}

    monkeypatch.setattr(index.tempfile, "mkdtemp", create_directory)
    monkeypatch.setattr(index, "prepare_download", prepare)
    messages = []

    async def receive():
        await asyncio.sleep(60)

    async def send(message):
        is_headers = message["type"] == "http.response.start"
        if (stage in {"headers", "cancel_headers"} and is_headers
                or stage in {"first_chunk", "cancel_chunk"} and not is_headers):
            if stage.startswith("cancel_"):
                raise asyncio.CancelledError()
            raise OSError("The browser disconnected")
        messages.append(message)

    async def scenario():
        response = await index.download_link(index.DownloadInput(
            url="https://media.example.com/owned.mp4", media_type="video"), ConnectedRequest())
        scope = {"type": "http", "method": "POST", "asgi": {"spec_version": "2.4"}}
        if stage == "complete":
            await response(scope, receive, send)
            assert messages[-1]["more_body"] is False
        else:
            expected = asyncio.CancelledError if stage.startswith("cancel_") else ClientDisconnect
            with pytest.raises(expected):
                await response(scope, receive, send)
        assert response.body_iterator.ag_frame is None
        assert directories and all(not directory.exists() for directory in directories)
        assert slots.acquire(blocking=False)
        assert slots.acquire(blocking=False)
        slots.release()
        slots.release()

    asyncio.run(scenario())


def test_older_asgi_disconnect_still_closes_download_resources():
    closed = []
    cleanups = []
    sent = asyncio.Event()

    async def content():
        try:
            yield b"owned media"
            await asyncio.sleep(60)
        finally:
            closed.append(True)

    async def send(message):
        if message["type"] == "http.response.body":
            sent.set()

    async def receive():
        await sent.wait()
        return {"type": "http.disconnect"}

    async def scenario():
        response = index.DownloadResponse(content(), cleanup=lambda: cleanups.append(True))
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        assert response.body_iterator.ag_frame is None
        assert closed == [True]
        assert cleanups == [True]

    asyncio.run(scenario())

