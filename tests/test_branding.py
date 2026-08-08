from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from baixatube import ui
from baixatube.branding import (
    APP_NAME,
    CREATOR_NAME,
    DISCORD_HANDLE,
    INSTAGRAM_HANDLE,
    INSTAGRAM_URL,
)
from baixatube.storage import migrate_legacy_database


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_about_dialog_shows_creator_avatar_and_contacts(monkeypatch):
    _app()
    monkeypatch.setattr(ui, "active_binary", lambda _name: None)
    opened: list[str] = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)

    dialog = ui.AboutDialog()

    assert APP_NAME in dialog.windowTitle()
    assert dialog.creator_name.text() == CREATOR_NAME
    assert INSTAGRAM_HANDLE in dialog.instagram_button.text()
    assert dialog.instagram_button.property("url") == INSTAGRAM_URL
    assert not dialog.instagram_button.icon().isNull()
    assert dialog.creator_avatar.pixmap() is not None
    assert not dialog.creator_avatar.pixmap().isNull()
    assert dialog.diagnostic_panel.isHidden()
    dialog.diagnostic_toggle.click()
    assert not dialog.diagnostic_panel.isHidden()
    assert "yt-dlp" in dialog.diagnostic_text.toPlainText()

    dialog.instagram_button.click()
    assert opened == [INSTAGRAM_URL]
    dialog.discord_button.click()
    assert QApplication.clipboard().text() == DISCORD_HANDLE
    assert DISCORD_HANDLE in dialog.discord_feedback.text()
    dialog.close()


def test_legacy_database_migration_is_atomic(tmp_path):
    source = tmp_path / "old" / "baixatube.db"
    source.parent.mkdir()
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.execute("INSERT INTO settings VALUES ('concurrency', '3')")
    destination = tmp_path / "new" / "braxytdow.db"

    assert migrate_legacy_database(destination, source)
    with sqlite3.connect(destination) as database:
        assert database.execute("SELECT value FROM settings WHERE key='concurrency'").fetchone()[0] == "3"
    assert not migrate_legacy_database(destination, source)
