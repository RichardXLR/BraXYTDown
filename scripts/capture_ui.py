"""Capture deterministic BraXYTDow UI states without network or tool bootstrap."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from baixatube import ui
from baixatube.compatibility import CompatibilityCheck, CompatibilityLevel, CompatibilityReport
from baixatube.models import DownloadProgress, DownloadRequest, DownloadStatus, FormatOption, HistoryEntry, MediaInfo
from baixatube.storage import Storage as RealStorage


def _fake_tool_setup(self: ui.MainWindow) -> None:
    self.analyze_button.setEnabled(True)
    states = {
        "yt-dlp": ("2026.08.06", "Mecanismo gerenciado pelo aplicativo"),
        "ffmpeg": ("8.1.2", "Conversão e pós-processamento prontos"),
        "deno": ("2.4.0", "Runtime de compatibilidade pronto"),
    }
    for name, (version, detail) in states.items():
        self.tool_cards[name].set_state(version, detail)
    self.tools_status_label.setText("Ferramentas prontas")
    self._health_checked(
        CompatibilityReport(
            [
                CompatibilityCheck("yt-dlp", CompatibilityLevel.HEALTHY, "Extração pública do YouTube operacional.", states["yt-dlp"][0]),
                CompatibilityCheck("deno", CompatibilityLevel.HEALTHY, "Runtime JavaScript operacional.", states["deno"][0]),
                CompatibilityCheck("ffmpeg", CompatibilityLevel.HEALTHY, "Conversão de mídia operacional.", states["ffmpeg"][0]),
            ]
        )
    )


def _populate(window: ui.MainWindow) -> None:
    window.url.setText("https://www.youtube.com/watch?v=demo")
    window._analyzed(
        MediaInfo(
            id="demo",
            title="Construindo uma experiência de download impecável",
            webpage_url=window.url.text(),
            uploader="Richard Ittou",
            duration=754,
            formats=[FormatOption("1080", "1080p MP4", "mp4", 1080)],
            subtitles=["pt-BR", "en"],
        )
    )

    requests = (
        DownloadRequest("demo-1", window.url.text(), "Construindo uma experiência de download impecável", str(Path.home() / "Downloads")),
        DownloadRequest("demo-2", window.url.text(), "Mix de áudio para foco e produtividade", str(Path.home() / "Downloads"), media_type="audio", output_format="mp3"),
        DownloadRequest("demo-3", window.url.text(), "Guia rápido de edição profissional", str(Path.home() / "Downloads"), quality="720"),
    )
    states = (DownloadStatus.DOWNLOADING, DownloadStatus.WAITING, DownloadStatus.COMPLETED)
    for request, state in zip(requests, states, strict=True):
        window.manager.requests[request.id] = request
        window.manager.statuses[request.id] = state
    window._sync_rows()
    window._update_row("demo-1", DownloadProgress(DownloadStatus.DOWNLOADING, 63.4, "8,4 MB/s", "00:18", "118 MB", "186 MB", "Transferindo vídeo 1080p"))
    window._update_row("demo-2", DownloadProgress(DownloadStatus.WAITING, message="Aguardando uma vaga"))
    window._update_row("demo-3", DownloadProgress(DownloadStatus.COMPLETED, 100, message=str(Path.home() / "Downloads" / "guia.mp4")))

    for entry in (
        HistoryEntry("h1", "Guia rápido de edição profissional", window.url.text(), str(Path.home() / "Downloads" / "guia.mp4"), "completed", "2026-08-07T14:22:00"),
        HistoryEntry("h2", "Mix de áudio para foco e produtividade", window.url.text(), str(Path.home() / "Downloads" / "mix.mp3"), "completed", "2026-08-07T12:04:00"),
        HistoryEntry("h3", "Vídeo público indisponível", window.url.text(), "", "error", "2026-08-06T20:18:00"),
    ):
        window.storage.add_history(entry)
    window._load_history()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--page", type=int, default=0, choices=range(4))
    parser.add_argument("--dialog", choices=("about", "settings", "session", "advanced"))
    parser.add_argument("--width", type=int, default=1366)
    parser.add_argument("--height", type=int, default=820)
    parser.add_argument("--dialog-width", type=int)
    parser.add_argument("--dialog-height", type=int)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--about-expanded", action="store_true")
    args = parser.parse_args()

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(ui.STYLE)
    with tempfile.TemporaryDirectory(prefix="braxytdow-ui-") as temporary:
        ui.Storage = lambda: RealStorage(Path(temporary) / "preview.db")  # type: ignore[assignment]
        ui.MainWindow._setup_tool_updates = _fake_tool_setup  # type: ignore[method-assign]
        window = ui.MainWindow()
        window.settings["native_notifications"] = False
        window.settings["completion_action"] = "none"
        _populate(window)
        window.resize(args.width, args.height)
        window._show_page(args.page)
        window.show()
        if args.compact:
            window.compact_mode_button.setChecked(True)
        QTest.qWait(320)
        target = window
        if args.dialog == "about":
            target = ui.AboutDialog(window)
            target.show()
        elif args.dialog == "settings":
            target = ui.SettingsDialog(window.settings, window)
            target.show()
        elif args.dialog == "session":
            target = ui.SettingsDialog(window.settings, window, initial_tab="session")
            target.show()
        elif args.dialog == "advanced":
            target = ui.AdvancedOptionsDialog(window._advanced, window)
            target.show()
        if target is not window:
            if args.dialog_width or args.dialog_height:
                target.resize(args.dialog_width or target.width(), args.dialog_height or target.height())
            if args.about_expanded and isinstance(target, ui.AboutDialog):
                target.diagnostic_toggle.setChecked(True)
            QTest.qWait(180)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not target.grab().save(str(args.output)):
            raise RuntimeError(f"Não foi possível salvar {args.output}")
        window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
