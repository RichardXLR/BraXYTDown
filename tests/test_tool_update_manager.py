from __future__ import annotations

import threading
import time

import pytest


QtCore = pytest.importorskip("PySide6.QtCore")

from baixatube.tool_updates import ToolState, ToolUpdateManager


class BlockingBootstrapUpdater:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.state = ToolState("yt-dlp", "2026.08.06", "C:/tools/yt-dlp.exe", "bundled")

    def bootstrap(self) -> list[ToolState]:
        self.entered.set()
        self.release.wait(timeout=2)
        return [self.state]


def test_bootstrap_async_runs_outside_caller_and_emits_finished_states():
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    updater = BlockingBootstrapUpdater()
    manager = ToolUpdateManager(updater=updater)
    operations: list[str] = []
    results: list[object] = []
    manager.started.connect(operations.append)
    manager.finished.connect(results.append)

    assert manager.bootstrap_async() is True
    assert updater.entered.wait(timeout=1)
    assert manager.busy is True
    assert results == []
    assert manager.bootstrap_async() is False

    updater.release.set()
    deadline = time.monotonic() + 2
    while (not results or manager.busy) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert operations == ["bootstrap"]
    assert results == [[updater.state]]
    assert manager.busy is False
    QtCore.QThreadPool.globalInstance().waitForDone(1000)
