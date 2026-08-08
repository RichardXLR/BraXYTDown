from __future__ import annotations

import subprocess

from baixatube import process_tree
from baixatube.process_tree import ProcessTreeGuard


class FakeJobApi:
    def __init__(
        self,
        *,
        handle: int | None = 91,
        assigned: bool = True,
    ) -> None:
        self.handle = handle
        self.assigned = assigned
        self.created = 0
        self.assignments: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def create_kill_on_close_job(self) -> int | None:
        self.created += 1
        return self.handle

    def assign(self, job_handle: int, pid: int) -> bool:
        self.assignments.append((job_handle, pid))
        return self.assigned

    def close(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


def test_attached_windows_job_terminates_by_closing_handle() -> None:
    api = FakeJobApi()
    fallback_calls: list[int] = []
    guard = ProcessTreeGuard(
        is_windows=True,
        api=api,
        fallback=lambda pid: fallback_calls.append(pid) is None,
    )

    assert guard.attach(4242)
    assert guard.attached
    assert guard.terminate()

    assert api.assignments == [(91, 4242)]
    assert api.closed == [91]
    assert fallback_calls == [4242]
    guard.close()
    assert api.closed == [91]


def test_assignment_failure_releases_job_then_uses_tree_fallback() -> None:
    api = FakeJobApi(assigned=False)
    fallback_calls: list[int] = []
    guard = ProcessTreeGuard(
        is_windows=True,
        api=api,
        fallback=lambda pid: not fallback_calls.append(pid),
    )

    assert not guard.attach(7331)
    assert api.closed == [91]
    assert guard.terminate()
    assert fallback_calls == [7331]
    assert api.closed == [91]


def test_windows_fallback_runs_before_closing_own_job() -> None:
    api = FakeJobApi()
    events: list[str] = []

    def close(handle: int) -> bool:
        events.append(f"close:{handle}")
        return True

    api.close = close  # type: ignore[method-assign]
    guard = ProcessTreeGuard(
        is_windows=True,
        api=api,
        fallback=lambda pid: not events.append(f"fallback:{pid}"),
    )

    assert guard.attach(6789)
    assert guard.terminate()
    assert events == ["fallback:6789", "close:91"]


def test_termination_before_process_has_pid_preserves_job_for_started_callback() -> None:
    api = FakeJobApi()
    guard = ProcessTreeGuard(is_windows=True, api=api)

    assert not guard.terminate()
    assert api.closed == []
    assert guard.attach(8080)
    assert guard.terminate()
    assert api.closed == [91]


def test_non_windows_guard_is_portable_no_op() -> None:
    api = FakeJobApi()
    fallback_calls: list[int] = []
    guard = ProcessTreeGuard(
        is_windows=False,
        api=api,
        fallback=lambda pid: not fallback_calls.append(pid),
    )

    assert not guard.attach(5150)
    assert not guard.terminate()
    assert api.created == 0
    assert api.assignments == []
    assert api.closed == []
    assert fallback_calls == []


def test_taskkill_fallback_uses_absolute_binary_and_no_shell(tmp_path, monkeypatch) -> None:
    system32 = tmp_path / "System32"
    system32.mkdir()
    executable = system32 / "taskkill.exe"
    executable.write_bytes(b"MZ")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(process_tree.subprocess, "run", fake_run)

    assert process_tree._taskkill_tree(2468)
    arguments, kwargs = calls[0]
    assert arguments == [str(executable), "/PID", "2468", "/T", "/F"]
    assert kwargs["check"] is False
    assert "shell" not in kwargs


def test_taskkill_never_targets_current_process(monkeypatch) -> None:
    monkeypatch.setattr(process_tree.os, "getpid", lambda: 1234)
    assert not process_tree._taskkill_tree(1234)
