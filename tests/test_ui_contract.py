from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from baixatube import ui
from baixatube.models import MediaInfo
from baixatube.storage import DEFAULT_SETTINGS, Storage as RealStorage


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(ui.STYLE)
    yield app


def _skip_tool_bootstrap(self: ui.MainWindow) -> None:
    self.analyze_button.setEnabled(True)


def test_editing_analyzed_url_invalidates_stale_media(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ui, "Storage", lambda: RealStorage(tmp_path / "ui.db"))
    monkeypatch.setattr(ui.MainWindow, "_setup_tool_updates", _skip_tool_bootstrap)
    window = ui.MainWindow()
    try:
        original = "https://www.youtube.com/watch?v=original"
        window.url.setText(original)
        window._analyzed(MediaInfo("original", "Prévia original", original))

        assert window.media is not None
        assert window.add_button.isEnabled()

        window.url.setText("https://www.youtube.com/watch?v=novo")

        assert window.media is None
        assert not window.add_button.isEnabled()
        assert window.preview_badge.property("state") == "warning"
        assert window.flow_rail.active_stage == 0
    finally:
        window.close()


def test_visual_contract_has_expanded_tokens_and_reduced_motion_setting():
    assert "${" not in ui.STYLE
    assert "TransferRail" in ui.__dict__
    assert DEFAULT_SETTINGS["interface_animations"] is True
    assert ui.STATUS_LABELS[ui.DownloadStatus.COMPLETED] == "Concluído"
