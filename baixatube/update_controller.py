from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .tool_updates import ToolUpdateManager, UpdateBatch, UpdatePreferences, UpdateResult

logger = logging.getLogger(__name__)


class UpdateController(QObject):
    """Coordinates background tool updates with the desktop UI."""

    status_changed = Signal(str, int)
    states_changed = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = ToolUpdateManager(parent=self)
        self._after_repair: list[Callable[[], None]] = []
        self.manager.progress.connect(self._progress)
        self.manager.finished.connect(self._finished)
        self.manager.failed.connect(self._failed)
        self.manager.busy_changed.connect(lambda busy: self.status_changed.emit("Verificando ferramentas…" if busy else "Ferramentas prontas", -1))

    @property
    def busy(self) -> bool:
        return self.manager.busy

    def bootstrap(self) -> list:
        states = self.manager.bootstrap()
        self.states_changed.emit(states)
        return states

    def schedule_automatic(self, settings: dict, delay_ms: int = 2500) -> None:
        preferences = UpdatePreferences.from_settings(settings)
        QTimer.singleShot(delay_ms, lambda: self.manager.check_and_update_async(preferences))

    def check_now(self) -> bool:
        return self.manager.check_async(force=True)

    def update_now(self, tools: tuple[str, ...] | None = None, force: bool = False) -> bool:
        return self.manager.update_async(tools, force=force)

    def repair_compatibility(self, callback: Callable[[], None] | None = None) -> bool:
        if callback:
            self._after_repair.append(callback)
        started = self.manager.update_async(("yt-dlp", "deno"), force=True)
        if not started and callback:
            self._after_repair.remove(callback)
        return started

    def status(self) -> list:
        return self.manager.status()

    def _progress(self, tool: str, percent: int, message: str) -> None:
        label = {"yt-dlp": "yt-dlp", "ffmpeg": "FFmpeg", "deno": "Deno"}.get(tool, tool)
        self.status_changed.emit(f"{label}: {message}", percent)

    def _finished(self, result: object) -> None:
        self.states_changed.emit(self.manager.status())
        callbacks = self._after_repair
        self._after_repair = []
        succeeded = True
        results = result.results if isinstance(result, UpdateBatch) else result if isinstance(result, list) else []
        for item in results:
            if isinstance(item, UpdateResult) and item.error:
                succeeded = False
                break
        if succeeded:
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    logger.exception("Falha ao retomar operação após reparar ferramentas")
        self.finished.emit(result)

    def _failed(self, message: str) -> None:
        self._after_repair = []
        logger.warning("Atualização de ferramentas falhou: %s", message)
        self.failed.emit(message)
