from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication

from baixatube.models import DownloadProgress, DownloadRequest, DownloadStatus
from baixatube.service import DownloadJob, DownloadManager, friendly_error


class FakeStorage:
    def __init__(self) -> None:
        self.saved: list[tuple[str, DownloadStatus]] = []
        self.removed: list[str] = []

    def recoverable_queue(self):
        return []

    def save_queue_item(self, request, status):
        self.saved.append((request.id, status))

    def remove_queue_item(self, request_id):
        self.removed.append(request_id)

    def add_history(self, _entry):
        pass


class FakeJob:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class FakeProcessTree:
    def __init__(self) -> None:
        self.attached: list[int] = []
        self.terminate_calls = 0
        self.close_calls = 0

    def attach(self, pid: int) -> bool:
        self.attached.append(pid)
        return True

    def terminate(self) -> bool:
        self.terminate_calls += 1
        return True

    def close(self) -> None:
        self.close_calls += 1


class FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.kill_calls = 0

    def processId(self) -> int:
        return self.pid

    def state(self):
        return 2  # QProcess.Running

    def kill(self) -> None:
        self.kill_calls += 1


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _request(tmp_path: Path, request_id: str = "item") -> DownloadRequest:
    return DownloadRequest(request_id, "https://youtu.be/example", "Teste", str(tmp_path))


def test_cookie_errors_are_explained_without_raw_credentials():
    message = friendly_error("ERROR: failed to decrypt cookies database at C:/Users/name/profile")
    assert "Feche o navegador" in message
    assert "C:/Users" not in message

    expired = friendly_error("ERROR: cookies are no longer valid")
    assert "sessão de cookies expirou" in expired.lower()


def test_download_job_buffers_partial_process_lines(tmp_path):
    _app()
    job = DownloadJob(_request(tmp_path))
    events: list[DownloadProgress] = []
    job.progress.connect(lambda _request_id, event: events.append(event))

    job._feed("stderr", "BT_PROGRESS|12")
    assert events == []
    job._feed("stderr", ".5%|1MiB/s|00:10|1MiB|8MiB\n")

    assert len(events) == 1
    assert events[0].status == DownloadStatus.DOWNLOADING
    assert events[0].percent == 12.5

    job._feed("stdout", "BT_F")
    assert job.output_path == ""
    job._feed("stdout", "ILE:C:/Downloads/video.mp4\n")
    assert job.output_path == "C:/Downloads/video.mp4"

    encoded = "BT_FILE:C:/Downloads/vídeo.mp4\n".encode("utf-8")
    split = encoded.index(b"\xc3") + 1
    job._feed("stdout", job._stdout_decoder.decode(encoded[:split], final=False))
    job._feed("stdout", job._stdout_decoder.decode(encoded[split:], final=False))
    assert job.output_path == "C:/Downloads/vídeo.mp4"


def test_download_job_attaches_and_terminates_entire_process_tree(tmp_path):
    _app()
    job = DownloadJob(_request(tmp_path))
    process = FakeProcess()
    tree = FakeProcessTree()
    job.process = process  # type: ignore[assignment]
    job._process_tree = tree  # type: ignore[assignment]

    job._process_started()
    job.stop()

    assert tree.attached == [4242]
    assert tree.terminate_calls == 1
    assert process.kill_calls == 1


def test_late_job_callbacks_after_remove_are_ignored(tmp_path):
    _app()
    storage = FakeStorage()
    manager = DownloadManager(storage)  # type: ignore[arg-type]
    request = _request(tmp_path)
    manager.add(request, start=False)
    job = FakeJob()
    manager.jobs[request.id] = job  # type: ignore[assignment]

    manager.remove(request.id)
    manager._job_progress(
        job,  # type: ignore[arg-type]
        request.id,
        DownloadProgress(DownloadStatus.DOWNLOADING, 50),
    )
    manager._job_finished(job, request.id, "ignored.mp4")  # type: ignore[arg-type]
    manager._job_failed(job, request.id, "ignored")  # type: ignore[arg-type]

    assert request.id not in manager.requests
    assert storage.removed == [request.id]
    assert job.stop_calls == 1


def test_pause_then_immediate_resume_does_not_strand_request(tmp_path):
    _app()
    storage = FakeStorage()
    manager = DownloadManager(storage)  # type: ignore[arg-type]
    request = _request(tmp_path)
    manager.add(request, start=False)
    job = FakeJob()
    manager.jobs[request.id] = job  # type: ignore[assignment]
    manager.statuses[request.id] = DownloadStatus.DOWNLOADING
    restarted: list[str] = []
    manager._start = restarted.append  # type: ignore[method-assign]

    manager.pause(request.id)
    manager.resume(request.id)
    manager._job_stopped(job, request.id)  # type: ignore[arg-type]

    assert job.stop_calls == 1
    assert manager.statuses[request.id] == DownloadStatus.WAITING
    assert restarted == [request.id]
    assert storage.saved[-1] == (request.id, DownloadStatus.WAITING)


def test_reducing_concurrency_never_uses_negative_slice_to_start_jobs(tmp_path):
    _app()
    storage = FakeStorage()
    manager = DownloadManager(storage, concurrency=1)  # type: ignore[arg-type]
    waiting = _request(tmp_path, "waiting")
    manager.add(waiting, start=False)
    manager.jobs = {"active-1": FakeJob(), "active-2": FakeJob()}  # type: ignore[assignment]
    started: list[str] = []
    manager._start = started.append  # type: ignore[method-assign]

    manager.pump()

    assert started == []


def test_restore_pumps_recoverable_requests(tmp_path):
    _app()
    storage = FakeStorage()
    request = _request(tmp_path)
    storage.recoverable_queue = lambda: [request]  # type: ignore[method-assign]
    manager = DownloadManager(storage)  # type: ignore[arg-type]
    started: list[str] = []
    manager._start = started.append  # type: ignore[method-assign]

    manager.restore()

    assert started == [request.id]
