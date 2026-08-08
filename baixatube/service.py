from __future__ import annotations

import codecs
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .command_builder import build_analysis_args, build_download_args
from .cookie_auth import CookieConfig
from .models import DownloadProgress, DownloadRequest, DownloadStatus, HistoryEntry, MediaInfo
from .network_policy import is_metered_connection
from .parsers import parse_media_json, parse_progress_line
from .paths import find_binary
from .process_tree import ProcessTreeGuard
from .storage import Storage
from .utils import has_free_space

logger = logging.getLogger(__name__)


def process_environment() -> QProcessEnvironment:
    """Expose managed tools to yt-dlp without changing the user's PATH."""
    environment = QProcessEnvironment.systemEnvironment()
    environment.insert("PYTHONUTF8", "1")
    paths: list[str] = []
    for name in ("ffmpeg", "deno"):
        binary = find_binary(name)
        if binary:
            parent = str(Path(binary).parent)
            if parent not in paths:
                paths.append(parent)
    current = environment.value("PATH")
    environment.insert("PATH", os.pathsep.join(paths + ([current] if current else [])))
    return environment


def friendly_error(raw: str) -> str:
    value = raw.lower()
    if any(marker in value for marker in ("failed to decrypt", "could not copy", "cookies database", "cookie file")):
        return "Não foi possível ler a sessão do navegador. Feche o navegador ou selecione um arquivo Netscape válido em Configurações > Sessão."
    if "cookies are no longer valid" in value or "cookies have expired" in value:
        return "A sessão de cookies expirou. Exporte uma sessão nova e tente novamente."
    if "private video" in value:
        return "Este vídeo é privado. Use somente uma sessão que tenha acesso legítimo ao conteúdo."
    if "not available in your country" in value or "geo" in value and "restricted" in value:
        return "Este conteúdo não está disponível na sua região."
    if "video unavailable" in value:
        return "O vídeo não está disponível."
    if "sign in to confirm" in value or "not a bot" in value:
        return "O YouTube pediu confirmação de sessão. Aguarde ou configure cookies em Configurações > Sessão, usando apenas conteúdo autorizado."
    if any(marker in value for marker in ("signature extraction failed", "nsig extraction failed", "javascript runtime", "challenge solving failed", "po token", "http error 403")):
        return "O YouTube mudou ou recusou a cadeia pública de reprodução. O AutoCura verificará yt-dlp, Deno/EJS e a versão anterior."
    if "requested format is not available" in value:
        return "A qualidade ou o formato solicitado não está disponível."
    if "unable to download" in value or "network" in value or "timed out" in value:
        return "Falha de rede durante o acesso ao conteúdo."
    cleaned = raw.strip().splitlines()
    return cleaned[-1][:500] if cleaned else "O processo terminou com um erro desconhecido."


class Analyzer(QObject):
    completed = Signal(object)
    failed = Signal(str)
    compatibility_issue = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        po_token_provider: bool = False,
        cookie_config: CookieConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.process: QProcess | None = None
        self.output = ""
        self.errors = ""
        self._cancelled = False
        self._terminal_emitted = False
        self._process_tree: ProcessTreeGuard | None = None
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.po_token_provider = po_token_provider
        self.cookie_config = cookie_config or CookieConfig()

    def analyze(self, url: str) -> None:
        if self.process and self.process.state() != QProcess.NotRunning:
            self.failed.emit("Já existe uma análise em andamento.")
            return
        binary = find_binary("yt-dlp")
        if not binary:
            self.failed.emit("yt-dlp não foi encontrado. Execute scripts\\prepare_binaries.ps1.")
            return
        self.output = ""
        self.errors = ""
        self._cancelled = False
        self._terminal_emitted = False
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._release_process_tree()
        self._process_tree = ProcessTreeGuard()
        try:
            cookie = self.cookie_config
            arguments = build_analysis_args(
                url,
                po_token_provider=self.po_token_provider,
                cookie_mode=cookie.mode,
                cookie_browser=cookie.browser,
                cookie_profile=cookie.profile,
                cookie_file=cookie.file_path,
                cookie_consent=cookie.consent,
            )
        except ValueError as exc:
            self._release_process_tree()
            self.failed.emit(str(exc))
            return
        self.process = QProcess(self)
        self.process.setProgram(binary)
        self.process.setArguments(arguments)
        self.process.setProcessEnvironment(process_environment())
        self.process.started.connect(self._process_started)
        self.process.readyReadStandardOutput.connect(self._stdout)
        self.process.readyReadStandardError.connect(self._stderr)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self.process.start()

    def cancel(self) -> None:
        if self.process and self.process.state() != QProcess.NotRunning:
            self._cancelled = True
            if self._process_tree:
                self._process_tree.terminate()
            self.process.kill()

    def _process_started(self) -> None:
        process = self.process
        if not process:
            return
        if self._process_tree:
            self._process_tree.attach(int(process.processId()))
        # Cancellation can arrive while QProcess is still in Starting state,
        # before it exposes a PID. Attach first and terminate immediately so
        # descendants cannot escape between those two events.
        if self._cancelled:
            if self._process_tree:
                self._process_tree.terminate()
            process.kill()

    def _release_process_tree(self) -> None:
        guard = self._process_tree
        self._process_tree = None
        if guard:
            guard.close()

    def _stdout(self) -> None:
        if self.process:
            chunk = bytes(self.process.readAllStandardOutput())
            self.output += self._stdout_decoder.decode(chunk, final=False)

    def _stderr(self) -> None:
        if self.process:
            chunk = bytes(self.process.readAllStandardError())
            self.errors += self._stderr_decoder.decode(chunk, final=False)

    def _finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        # QProcess may still have unread bytes when ``finished`` is delivered.
        self._stdout()
        self._stderr()
        self.output += self._stdout_decoder.decode(b"", final=True)
        self.errors += self._stderr_decoder.decode(b"", final=True)
        process = self.process
        self.process = None
        self._release_process_tree()
        if process:
            process.deleteLater()
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        if self._cancelled:
            self.failed.emit("Análise cancelada.")
            return
        if code:
            logger.warning("Falha na análise: %s", friendly_error(self.errors or self.output))
            message = friendly_error(self.errors or self.output)
            if message.startswith("O YouTube mudou"):
                self.compatibility_issue.emit(message)
            self.failed.emit(message)
            return
        try:
            from .parsers import parse_media_json
            self.completed.emit(parse_media_json(self.output))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.failed.emit(f"Não foi possível interpretar os dados do vídeo: {exc}")

    def _process_error(self, error: QProcess.ProcessError) -> None:
        # FailedToStart is not guaranteed to be followed by ``finished``.
        if error != QProcess.ProcessError.FailedToStart or self._terminal_emitted:
            return
        self._terminal_emitted = True
        process = self.process
        message = process.errorString() if process else "erro desconhecido"
        self.process = None
        self._release_process_tree()
        if process:
            process.deleteLater()
        self.failed.emit(f"Não foi possível iniciar o yt-dlp: {message}")


class DownloadJob(QObject):
    progress = Signal(str, object)
    finished = Signal(str, str)
    failed = Signal(str, str)
    stopped = Signal(str)

    def __init__(self, request: DownloadRequest, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.request = request
        self.process: QProcess | None = None
        self.output_path = ""
        self.errors = ""
        self._intentional_stop = False
        self._terminal_emitted = False
        self._process_tree: ProcessTreeGuard | None = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._postprocessing = False
        self._postprocess_temp: Path | None = None

    def start(self) -> None:
        if self._terminal_emitted:
            return
        # A queued start can race with a cancel/remove action.  Never reset this
        # flag here or a cancelled job can launch after the user removed it.
        if self._intentional_stop:
            self._emit_stopped()
            return
        try:
            binary = find_binary("yt-dlp")
            if not binary:
                self._emit_failed("yt-dlp não foi encontrado.")
                return
            if not has_free_space(Path(self.request.destination), None):
                self._emit_failed("Não há espaço livre suficiente na pasta de destino.")
                return
            self._release_process_tree()
            self._process_tree = ProcessTreeGuard()
            self.process = QProcess(self)
            self.process.setProgram(binary)
            self.process.setArguments(build_download_args(self.request))
            self.process.setProcessEnvironment(process_environment())
            self.process.setProcessChannelMode(QProcess.SeparateChannels)
            self.process.started.connect(self._process_started)
            self.process.readyReadStandardOutput.connect(self._read_stdout)
            self.process.readyReadStandardError.connect(self._read_stderr)
            self.process.errorOccurred.connect(self._process_error)
            self.process.finished.connect(self._on_finished)
            self.process.start()
        except (OSError, ValueError) as exc:
            self._release_process_tree()
            self._emit_failed(f"Não foi possível preparar o download: {exc}")

    def stop(self) -> None:
        self._intentional_stop = True
        if self.process and self.process.state() != QProcess.NotRunning:
            if self._process_tree:
                self._process_tree.terminate()
            self.process.kill()

    def _process_started(self) -> None:
        process = self.process
        if not process:
            return
        if self._process_tree:
            self._process_tree.attach(int(process.processId()))
        if self._intentional_stop:
            if self._process_tree:
                self._process_tree.terminate()
            process.kill()

    def _release_process_tree(self) -> None:
        guard = self._process_tree
        self._process_tree = None
        if guard:
            guard.close()

    def _consume_line(self, line: str) -> None:
        event = parse_progress_line(line)
        if event:
            if event.status == DownloadStatus.COMPLETED and event.message:
                self.output_path = event.message
                return
            self.progress.emit(self.request.id, event)

    def _feed(self, channel: str, text: str, *, flush: bool = False) -> None:
        """Consume complete lines while retaining a partial QProcess chunk."""

        attribute = f"_{channel}_buffer"
        combined = getattr(self, attribute) + text
        if flush:
            lines = combined.splitlines()
            remainder = ""
        else:
            lines = combined.splitlines(keepends=True)
            remainder = ""
            if lines and not lines[-1].endswith(("\n", "\r")):
                remainder = lines.pop()
        setattr(self, attribute, remainder)
        for line in lines:
            self._consume_line(line.rstrip("\r\n"))

    def _read_stdout(self) -> None:
        if self.process:
            chunk = bytes(self.process.readAllStandardOutput())
            self._feed("stdout", self._stdout_decoder.decode(chunk, final=False))

    def _read_stderr(self) -> None:
        if self.process:
            raw = bytes(self.process.readAllStandardError())
            chunk = self._stderr_decoder.decode(raw, final=False)
            self.errors = (self.errors + chunk)[-64 * 1024:]
            self._feed("stderr", chunk)

    def _on_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        self._read_stdout()
        self._read_stderr()
        self._feed("stdout", self._stdout_decoder.decode(b"", final=True), flush=True)
        final_stderr = self._stderr_decoder.decode(b"", final=True)
        self.errors = (self.errors + final_stderr)[-64 * 1024:]
        self._feed("stderr", final_stderr, flush=True)
        process = self.process
        self.process = None
        self._release_process_tree()
        if process:
            process.deleteLater()
        if self._terminal_emitted or self._postprocessing:
            return
        if self._intentional_stop:
            self._emit_stopped()
        elif code == 0:
            if self._needs_metadata_postprocess() and self._start_metadata_postprocess():
                return
            self._emit_finished()
        else:
            logger.warning("Download %s falhou: %s", self.request.id, friendly_error(self.errors))
            self._emit_failed(friendly_error(self.errors))

    def _needs_metadata_postprocess(self) -> bool:
        return bool(
            self.output_path
            and any(
                (
                    self.request.custom_title,
                    self.request.custom_artist,
                    self.request.custom_album,
                    self.request.custom_cover,
                )
            )
        )

    def _start_metadata_postprocess(self) -> bool:
        source = Path(self.output_path)
        ffmpeg = find_binary("ffmpeg")
        if not source.is_file() or not ffmpeg:
            return False
        temporary = source.with_name(f".{source.stem}.braxy-meta-{uuid.uuid4().hex[:8]}{source.suffix}")
        args = ["-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        cover = Path(self.request.custom_cover) if self.request.custom_cover else None
        cover_supported = bool(cover and cover.is_file() and source.suffix.casefold() in {".mp3", ".m4a", ".mp4"})
        if cover_supported:
            args += ["-i", str(cover)]
            if source.suffix.casefold() == ".mp3":
                args += ["-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "mjpeg", "-id3v2_version", "3"]
            else:
                cover_stream = "v:1" if source.suffix.casefold() == ".mp4" else "v:0"
                args += ["-map", "0", "-map", "1:v", "-c", "copy", f"-disposition:{cover_stream}", "attached_pic"]
        else:
            args += ["-map", "0", "-c", "copy"]
        for key, value in (
            ("title", self.request.custom_title),
            ("artist", self.request.custom_artist),
            ("album", self.request.custom_album),
        ):
            if value.strip():
                args += ["-metadata", f"{key}={value.strip()[:300]}"]
        args.append(str(temporary))
        self._postprocessing = True
        self._postprocess_temp = temporary
        self.progress.emit(self.request.id, DownloadProgress(DownloadStatus.CONVERTING, 99, message="Aplicando nome, metadados e capa…"))
        self._process_tree = ProcessTreeGuard()
        self.process = QProcess(self)
        self.process.setProgram(ffmpeg)
        self.process.setArguments(args)
        self.process.setProcessEnvironment(process_environment())
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.started.connect(self._process_started)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._metadata_finished)
        self.process.start()
        return True

    def _metadata_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        self._read_stderr()
        process = self.process
        self.process = None
        self._release_process_tree()
        if process:
            process.deleteLater()
        temporary = self._postprocess_temp
        self._postprocessing = False
        self._postprocess_temp = None
        if self._intentional_stop:
            if temporary:
                temporary.unlink(missing_ok=True)
            self._emit_stopped()
            return
        if code == 0 and temporary and temporary.is_file():
            try:
                os.replace(temporary, self.output_path)
                self._emit_finished()
                return
            except OSError as exc:
                self.errors += f"\n{exc}"
        if temporary:
            temporary.unlink(missing_ok=True)
        self._emit_failed("O arquivo foi baixado, mas não foi possível finalizar os metadados personalizados.")

    def _emit_finished(self) -> None:
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self.finished.emit(self.request.id, self.output_path)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart or self._terminal_emitted:
            return
        process = self.process
        message = process.errorString() if process else "erro desconhecido"
        self.process = None
        self._release_process_tree()
        if process:
            process.deleteLater()
        if self._intentional_stop:
            self._emit_stopped()
        else:
            self._emit_failed(f"Não foi possível iniciar o yt-dlp: {message}")

    def _emit_failed(self, message: str) -> None:
        if self._terminal_emitted:
            return
        if self._postprocess_temp:
            self._postprocess_temp.unlink(missing_ok=True)
            self._postprocess_temp = None
        self._postprocessing = False
        self._release_process_tree()
        self._terminal_emitted = True
        self.failed.emit(self.request.id, message)

    def _emit_stopped(self) -> None:
        if self._terminal_emitted:
            return
        self._release_process_tree()
        self._terminal_emitted = True
        self.stopped.emit(self.request.id)


class DownloadManager(QObject):
    updated = Signal(str, object)
    queue_changed = Signal()
    queue_idle = Signal()
    compatibility_issue = Signal(str, str)

    def __init__(
        self,
        storage: Storage,
        concurrency: int = 2,
        parent: QObject | None = None,
        *,
        settings: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.storage = storage
        self.concurrency = max(1, min(4, concurrency))
        self.requests: dict[str, DownloadRequest] = {}
        self.statuses: dict[str, DownloadStatus] = {}
        self.jobs: dict[str, DownloadJob] = {}
        self.paused: set[str] = set()
        self.attempts: dict[str, int] = {}
        self._pumping = False
        self._pump_requested = False
        self.settings = settings or {}
        self._metered_notice: set[str] = set()
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setSingleShot(True)
        self._schedule_timer.timeout.connect(self.pump)

    def restore(self) -> None:
        restored = False
        for request in self.storage.recoverable_queue():
            self.add(request, start=False)
            restored = True
        # The UI has no separate "start waiting item" action.  Without pumping
        # here, recovered requests remained permanently stranded after restart.
        if restored:
            self.pump()

    def add(self, request: DownloadRequest, start: bool = True) -> None:
        if not request.created_at:
            request.created_at = datetime.now(timezone.utc).isoformat()
        if not self.requests and request.order_index < 0:
            request.order_index = 0
        elif request.id not in self.requests and request.order_index <= 0:
            request.order_index = max((item.order_index for item in self.requests.values()), default=-1) + 1
        self.requests[request.id] = request
        self.statuses[request.id] = DownloadStatus.WAITING
        self.attempts.setdefault(request.id, 0)
        self.storage.save_queue_item(request, DownloadStatus.WAITING)
        self.updated.emit(request.id, DownloadProgress(DownloadStatus.WAITING))
        self.queue_changed.emit()
        if start:
            self.pump()

    def pump(self) -> None:
        if self._pumping:
            self._pump_requested = True
            return
        self._pumping = True
        try:
            available = max(0, self.concurrency - len(self.jobs))
            waiting = [
                key
                for key, value in self.statuses.items()
                if value == DownloadStatus.WAITING
                and key not in self.paused
                and key not in self.jobs
                and self._request_due(self.requests[key])
            ]
            waiting.sort(key=lambda key: (-max(0, min(2, self.requests[key].priority)), self.requests[key].order_index, self.requests[key].created_at, key))
            for request_id in waiting[:available]:
                self._start(request_id)
            future = [item for key, item in self.requests.items() if self.statuses.get(key) == DownloadStatus.WAITING and not self._request_due(item)]
            if future:
                self._schedule_timer.start(30_000)
            if not self.jobs and not waiting and not future:
                self.queue_idle.emit()
        finally:
            self._pumping = False
        if self._pump_requested:
            self._pump_requested = False
            QTimer.singleShot(0, self.pump)

    def _start(self, request_id: str) -> None:
        if (
            request_id not in self.requests
            or request_id in self.jobs
            or request_id in self.paused
            or self.statuses.get(request_id) != DownloadStatus.WAITING
        ):
            return
        request = self.requests[request_id]
        if request.pause_on_metered and is_metered_connection():
            if request_id not in self._metered_notice:
                self._metered_notice.add(request_id)
                self.updated.emit(request_id, DownloadProgress(DownloadStatus.WAITING, message="Aguardando uma conexão não limitada"))
            self._schedule_timer.start(30_000)
            return
        self._metered_notice.discard(request_id)
        job = DownloadJob(request, self)
        self.jobs[request_id] = job
        self.statuses[request_id] = DownloadStatus.DOWNLOADING
        self.storage.save_queue_item(request, DownloadStatus.DOWNLOADING)
        self.updated.emit(request_id, DownloadProgress(DownloadStatus.DOWNLOADING, message="Iniciando download…"))
        # Capture the job instance so a late signal from a removed/replaced job
        # cannot mutate the state of a newer request with the same id.
        job.progress.connect(lambda key, event, current=job: self._job_progress(current, key, event))
        job.finished.connect(lambda key, path, current=job: self._job_finished(current, key, path))
        job.failed.connect(lambda key, message, current=job: self._job_failed(current, key, message))
        job.stopped.connect(lambda key, current=job: self._job_stopped(current, key))
        job.finished.connect(job.deleteLater)
        job.failed.connect(job.deleteLater)
        job.stopped.connect(job.deleteLater)
        # Starting on the next event-loop turn prevents a synchronous preflight
        # failure from recursively entering this pump with a stale waiting list.
        QTimer.singleShot(0, job.start)

    def pause(self, request_id: str) -> None:
        if request_id not in self.requests:
            return
        self.paused.add(request_id)
        if request_id in self.jobs:
            self.jobs[request_id].stop()
        else:
            self.statuses[request_id] = DownloadStatus.WAITING
            self.storage.save_queue_item(self.requests[request_id], DownloadStatus.WAITING)
            self.updated.emit(request_id, DownloadProgress(DownloadStatus.WAITING, message="Pausado"))

    def resume(self, request_id: str) -> None:
        if request_id not in self.requests:
            return
        self.paused.discard(request_id)
        self.attempts[request_id] = 0
        if request_id in self.jobs or self.statuses.get(request_id) in {
            DownloadStatus.WAITING,
            DownloadStatus.CANCELLED,
            DownloadStatus.ERROR,
        }:
            self.statuses[request_id] = DownloadStatus.WAITING
            self.storage.save_queue_item(self.requests[request_id], DownloadStatus.WAITING)
            self.updated.emit(request_id, DownloadProgress(DownloadStatus.WAITING, message="Pronto para retomar"))
            self.pump()

    def cancel(self, request_id: str) -> None:
        if request_id not in self.requests:
            return
        self.paused.discard(request_id)
        self.attempts.pop(request_id, None)
        self.statuses[request_id] = DownloadStatus.CANCELLED
        self.storage.save_queue_item(self.requests[request_id], DownloadStatus.CANCELLED)
        self.updated.emit(request_id, DownloadProgress(DownloadStatus.CANCELLED))
        if request_id in self.jobs:
            self.jobs[request_id].stop()
        else:
            self.pump()

    def remove(self, request_id: str) -> None:
        job = self.jobs.pop(request_id, None)
        if job:
            job.stop()
        self.requests.pop(request_id, None)
        self.statuses.pop(request_id, None)
        self.paused.discard(request_id)
        self.attempts.pop(request_id, None)
        self.storage.remove_queue_item(request_id)
        self.queue_changed.emit()
        self.pump()

    def update_requests(self, request_ids: list[str], **changes: object) -> None:
        editable = {
            "destination",
            "quality",
            "output_format",
            "media_type",
            "priority",
            "scheduled_at",
            "rate_limit",
            "pause_on_metered",
        }
        for request_id in request_ids:
            request = self.requests.get(request_id)
            if not request or request_id in self.jobs:
                continue
            for key, value in changes.items():
                if key in editable and hasattr(request, key):
                    setattr(request, key, value)
            self.storage.save_queue_item(request, self.statuses.get(request_id, DownloadStatus.WAITING))
            self.updated.emit(request_id, DownloadProgress(self.statuses.get(request_id, DownloadStatus.WAITING), message="Configurações atualizadas"))
        self.queue_changed.emit()
        self.pump()

    def reorder(self, request_ids: list[str]) -> None:
        known = [key for key in request_ids if key in self.requests]
        missing = [key for key in self.requests if key not in known]
        ordered = known + missing
        for index, request_id in enumerate(ordered):
            self.requests[request_id].order_index = index
            self.storage.save_queue_item(self.requests[request_id], self.statuses.get(request_id, DownloadStatus.WAITING))
        self.storage.save_queue_order(ordered)
        self.queue_changed.emit()

    def _request_due(self, request: DownloadRequest) -> bool:
        if not request.scheduled_at:
            return True
        try:
            scheduled = datetime.fromisoformat(request.scheduled_at.replace("Z", "+00:00"))
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            return scheduled <= datetime.now(timezone.utc)
        except ValueError:
            return True

    def _job_progress(self, job: DownloadJob, request_id: str, event: DownloadProgress) -> None:
        if self.jobs.get(request_id) is not job or request_id not in self.requests:
            return
        previous = self.statuses.get(request_id)
        self.statuses[request_id] = event.status
        # Persist state transitions, not every progress line.  SQLite commits on
        # each percentage update used to block the GUI thread under load.
        if event.status != previous:
            self.storage.save_queue_item(self.requests[request_id], event.status)
        self.updated.emit(request_id, event)

    def _job_stopped(self, job: DownloadJob, request_id: str) -> None:
        if self.jobs.get(request_id) is not job:
            return
        self.jobs.pop(request_id, None)
        if request_id not in self.requests:
            self.pump()
            return
        if request_id in self.paused:
            self.statuses[request_id] = DownloadStatus.WAITING
            self.storage.save_queue_item(self.requests[request_id], DownloadStatus.WAITING)
            self.updated.emit(request_id, DownloadProgress(DownloadStatus.WAITING, message="Pausado; a retomada preservará o arquivo parcial"))
        elif self.statuses.get(request_id) == DownloadStatus.WAITING:
            # The user clicked resume before the killed process emitted
            # ``finished``.  Keep the request runnable instead of stranding it
            # in DOWNLOADING with no corresponding job.
            self.storage.save_queue_item(self.requests[request_id], DownloadStatus.WAITING)
            self.updated.emit(request_id, DownloadProgress(DownloadStatus.WAITING, message="Pronto para retomar"))
        self.pump()

    def _job_finished(self, job: DownloadJob, request_id: str, output_path: str) -> None:
        if self.jobs.get(request_id) is not job:
            return
        self.jobs.pop(request_id, None)
        if request_id not in self.requests or self.statuses.get(request_id) == DownloadStatus.CANCELLED:
            self.pump()
            return
        request = self.requests[request_id]
        completed_at = datetime.now(timezone.utc).isoformat()
        self.statuses[request_id] = DownloadStatus.COMPLETED
        self.attempts.pop(request_id, None)
        event = DownloadProgress(DownloadStatus.COMPLETED, 100, message=output_path)
        self.storage.save_queue_item(request, DownloadStatus.COMPLETED)
        self.storage.add_history(
            HistoryEntry(
                request_id,
                request.title,
                request.url,
                output_path,
                DownloadStatus.COMPLETED.value,
                completed_at,
                request.media_id,
                request.uploader,
                request.duration,
                request.media_type,
                request.output_format,
                request.quality,
                request.playlist_id,
                request.playlist_title,
                request.playlist_index,
                Path(output_path).stat().st_size if output_path and Path(output_path).is_file() else 0,
                "present" if output_path and Path(output_path).is_file() else "missing",
            )
        )
        self.storage.archive_playlist_items(request, output_path, completed_at)
        self.updated.emit(request_id, event)
        self.pump()

    def _job_failed(self, job: DownloadJob, request_id: str, message: str) -> None:
        if self.jobs.get(request_id) is not job:
            return
        self.jobs.pop(request_id, None)
        if request_id not in self.requests or self.statuses.get(request_id) == DownloadStatus.CANCELLED:
            self.pump()
            return
        request = self.requests[request_id]
        self.attempts[request_id] = self.attempts.get(request_id, 0) + 1
        if message.startswith("O YouTube mudou"):
            self.compatibility_issue.emit(request_id, message)
        transient = message.startswith("Falha de rede")
        if transient and self.attempts[request_id] <= 2:
            self.statuses[request_id] = DownloadStatus.WAITING
            self.storage.save_queue_item(request, DownloadStatus.WAITING)
            self.updated.emit(request_id, DownloadProgress(DownloadStatus.WAITING, message=f"Falha temporária; nova tentativa {self.attempts[request_id]}/2 em instantes"))
            QTimer.singleShot(2500, self.pump)
            return
        self.statuses[request_id] = DownloadStatus.ERROR
        self.storage.save_queue_item(request, DownloadStatus.ERROR)
        self.storage.add_history(
            HistoryEntry(
                request_id,
                request.title,
                request.url,
                "",
                DownloadStatus.ERROR.value,
                datetime.now(timezone.utc).isoformat(),
                request.media_id,
                request.uploader,
                request.duration,
                request.media_type,
                request.output_format,
                request.quality,
                request.playlist_id,
                request.playlist_title,
                request.playlist_index,
            )
        )
        self.updated.emit(request_id, DownloadProgress(DownloadStatus.ERROR, message=message))
        self.pump()
