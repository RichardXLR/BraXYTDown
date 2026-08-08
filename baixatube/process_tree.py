from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


# https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobApi(Protocol):
    def create_kill_on_close_job(self) -> int | None: ...

    def assign(self, job_handle: int, pid: int) -> bool: ...

    def close(self, handle: int) -> bool: ...


class _WindowsJobApi:
    """Minimal, lazily loaded Windows Job Object API."""

    def __init__(self) -> None:
        # WinDLL is intentionally resolved only on Windows so importing this
        # module remains harmless on macOS and Linux.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32

    def create_kill_on_close_job(self) -> int | None:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("CreateJobObjectW falhou (erro %s)", ctypes.get_last_error())
            return None
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = self._kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            logger.warning("SetInformationJobObject falhou (erro %s)", error)
            return None
        return int(handle)

    def assign(self, job_handle: int, pid: int) -> bool:
        process_handle = self._kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
            False,
            pid,
        )
        if not process_handle:
            logger.warning("OpenProcess(%s) falhou (erro %s)", pid, ctypes.get_last_error())
            return False
        try:
            assigned = self._kernel32.AssignProcessToJobObject(job_handle, process_handle)
            if not assigned:
                logger.warning(
                    "AssignProcessToJobObject(%s) falhou (erro %s)",
                    pid,
                    ctypes.get_last_error(),
                )
            return bool(assigned)
        finally:
            self._kernel32.CloseHandle(process_handle)

    def close(self, handle: int) -> bool:
        closed = bool(self._kernel32.CloseHandle(handle))
        if not closed:
            logger.warning("CloseHandle(Job Object) falhou (erro %s)", ctypes.get_last_error())
        return closed


def _taskkill_tree(pid: int) -> bool:
    """Best-effort Windows fallback without a shell or PATH lookup."""

    if pid <= 0 or pid == os.getpid():
        return False
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        return False
    executable = Path(system_root) / "System32" / "taskkill.exe"
    if not executable.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(executable), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        logger.warning("O fallback taskkill falhou para o PID %s", pid, exc_info=True)
        return False


class ProcessTreeGuard:
    """Own a child process tree and terminate it without platform surprises.

    On Windows, the child is assigned to a Job Object configured with
    ``KILL_ON_JOB_CLOSE``.  Closing the guard therefore also closes descendants
    launched by yt-dlp, such as FFmpeg and JavaScript runtimes.  On explicit
    termination an absolute, shell-free ``taskkill /T`` pass also catches a
    descendant created in the short interval before Job Object assignment, or
    when host policy refuses that assignment.  Other platforms intentionally
    remain no-op; QProcess keeps responsibility for its direct child there.
    """

    def __init__(
        self,
        *,
        is_windows: bool | None = None,
        api: _JobApi | None = None,
        fallback: Callable[[int], bool] | None = None,
    ) -> None:
        self._is_windows = sys.platform == "win32" if is_windows is None else is_windows
        self._api = api
        self._fallback = fallback or _taskkill_tree
        self._job_handle: int | None = None
        self._pid: int | None = None
        self._attached = False
        self._closed = False
        if not self._is_windows:
            return
        try:
            self._api = self._api or _WindowsJobApi()
            self._job_handle = self._api.create_kill_on_close_job()
        except (AttributeError, OSError):
            # Security products and compatibility layers can make kernel APIs
            # unavailable.  Keep running and retain the explicit fallback.
            logger.warning("Windows Job Objects não estão disponíveis", exc_info=True)
            self._api = None

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self, pid: int) -> bool:
        """Attach one newly started direct child to this guard."""

        if self._closed or pid <= 0 or pid == os.getpid():
            return False
        if self._pid is not None:
            return self._pid == pid and self._attached
        self._pid = pid
        if not self._is_windows or self._api is None or self._job_handle is None:
            return False
        try:
            self._attached = self._api.assign(self._job_handle, pid)
        except OSError:
            logger.warning("Não foi possível anexar o PID %s ao Job Object", pid, exc_info=True)
            self._attached = False
        if not self._attached:
            # An unassigned empty Job Object provides no protection.  Release
            # it now, while preserving the PID for taskkill on explicit stop.
            self._close_job()
        return self._attached

    def terminate(self) -> bool:
        """Terminate the attached tree, or use the Windows tree fallback."""

        if self._closed or self._pid is None:
            # A QProcess can still be in Starting state with no PID.  Do not
            # close the job yet: the started callback will attach and retry.
            return False
        terminated = False
        if self._attached and self._job_handle is not None:
            fallback_terminated = False
            if self._is_windows:
                try:
                    # A very fast launcher can create descendants before the
                    # QProcess.started callback assigns its PID to the job.
                    # Enumerate and terminate that tree while the root still
                    # exists; KILL_ON_JOB_CLOSE then guarantees cleanup for
                    # every process that was successfully associated.
                    fallback_terminated = bool(self._fallback(self._pid))
                except (OSError, subprocess.SubprocessError):
                    logger.warning("Não foi possível encerrar a árvore do PID %s", self._pid, exc_info=True)
            terminated = self._close_job() or fallback_terminated
            self._attached = False
        else:
            self._close_job()
        if not terminated and self._is_windows:
            try:
                terminated = bool(self._fallback(self._pid))
            except (OSError, subprocess.SubprocessError):
                logger.warning("Não foi possível encerrar a árvore do PID %s", self._pid, exc_info=True)
        self._closed = True
        return terminated

    def close(self) -> None:
        """Release ownership; KILL_ON_JOB_CLOSE clears any lingering child."""

        if self._closed:
            return
        self._close_job()
        self._attached = False
        self._closed = True

    def _close_job(self) -> bool:
        handle = self._job_handle
        self._job_handle = None
        if handle is None or self._api is None:
            return False
        try:
            return self._api.close(handle)
        except OSError:
            logger.warning("Não foi possível fechar o Job Object", exc_info=True)
            return False

    def __enter__(self) -> ProcessTreeGuard:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Never let a forgotten Python reference keep a Windows Job Object (and
        # its descendant processes) alive through application shutdown.
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown safeguard
            pass
