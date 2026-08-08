"""Signed application updates with channels, staged rollout and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .branding import APP_NAME
from .paths import data_dir
from .release_integrity import AuthenticodeResult, verify_authenticode


MAX_MANIFEST_BYTES = 256 * 1024
MAX_INSTALLER_BYTES = 320 * 1024 * 1024
CHANNELS = {"stable", "experimental"}


class AppUpdateError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class PreviousRelease:
    version: str
    installer_url: str
    sha256: str
    size: int
    publisher: str
    signer_sha256: str


@dataclass(slots=True, frozen=True)
class AppRelease:
    version: str
    installer_url: str
    sha256: str
    size: int
    publisher: str
    signer_sha256: str
    notes: str = ""
    channel: str = "stable"
    rollout: int = 100
    mandatory: bool = False
    published_at: str = ""
    previous: PreviousRelease | None = None


@dataclass(slots=True, frozen=True)
class AppUpdateCheck:
    current_version: str
    available: bool
    release: AppRelease | None = None
    message: str = ""
    error: str = ""


@dataclass(slots=True, frozen=True)
class AppUpdateResult:
    version: str
    path: str | None
    message: str
    error: str = ""


@dataclass(slots=True, frozen=True)
class StartupRecovery:
    action: str = "none"
    installer_path: str = ""
    message: str = ""


SignatureVerifier = Callable[[Path, str, str], AuthenticodeResult]


class AppUpdater:
    def __init__(
        self,
        manifest_url: str = "",
        *,
        channel: str = "stable",
        device_id: str = "",
        signature_verifier: SignatureVerifier = verify_authenticode,
    ) -> None:
        self.manifest_url = manifest_url.strip()
        self.channel = channel if channel in CHANNELS else "stable"
        self.device_id = device_id.strip() or "local-installation"
        self.signature_verifier = signature_verifier

    def check(self) -> AppUpdateCheck:
        if not self.manifest_url:
            return AppUpdateCheck(
                __version__,
                False,
                message="Canal do aplicativo ainda não publicado. Configure um manifesto HTTPS nas preferências.",
            )
        try:
            payload = json.loads(self._read(self.manifest_url, MAX_MANIFEST_BYTES).decode("utf-8"))
            release = self._parse_manifest(payload)
            newer = _is_newer(release.version, __version__)
            eligible = _rollout_eligible(self.device_id, release.version, release.channel, release.rollout)
            if newer and not eligible:
                return AppUpdateCheck(
                    __version__,
                    False,
                    release,
                    f"A versão {release.version} está em liberação gradual ({release.rollout}%). Este dispositivo ainda não foi selecionado.",
                )
            return AppUpdateCheck(
                __version__,
                newer,
                release,
                f"Versão {release.version} disponível no canal {release.channel}." if newer else "O aplicativo já está atualizado.",
            )
        except (AppUpdateError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return AppUpdateCheck(__version__, False, error=str(exc))

    def download(self, release: AppRelease, progress: Callable[[int], None] | None = None) -> AppUpdateResult:
        updates = _updates_dir()
        final_path = updates / f"{APP_NAME}-Setup-{release.version}-x64.exe"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".installer-", suffix=".download", dir=updates)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            digest, received = self._download(release.installer_url, temporary, progress)
            if digest != release.sha256:
                raise AppUpdateError("O SHA-256 do instalador não confere com o manifesto de publicação.")
            if received != release.size:
                raise AppUpdateError("O tamanho do instalador não confere com o manifesto de publicação.")
            signature = self.signature_verifier(temporary, release.publisher, release.signer_sha256)
            if not signature.valid:
                detail = signature.detail or signature.status
                raise AppUpdateError(f"A assinatura digital Authenticode não é válida ou não pertence ao criador. {detail}".strip())
            if release.previous and release.previous.version == __version__:
                self._cache_previous(release.previous)
            os.replace(temporary, final_path)
            self._record_verified(release, final_path)
            return AppUpdateResult(
                release.version,
                str(final_path),
                "Instalador, tamanho, SHA-256 e assinatura do criador foram verificados.",
            )
        except (AppUpdateError, OSError, urllib.error.URLError) as exc:
            return AppUpdateResult(release.version, None, "A atualização do aplicativo não foi preparada.", str(exc))
        finally:
            temporary.unlink(missing_ok=True)

    def rollback_candidate(self, *, include_current: bool = False) -> AppUpdateResult:
        entries = _read_json(_verified_index(), {}).get("installers", [])
        candidates = [
            item
            for item in entries
            if _is_newer(__version__, str(item.get("version", "")))
            or include_current and str(item.get("version", "")) == __version__
        ]
        candidates.sort(key=lambda item: _version_tuple(str(item.get("version", ""))), reverse=True)
        for item in candidates:
            try:
                path = Path(str(item["path"]))
                if not path.is_file() or path.stat().st_size != int(item["size"]):
                    continue
                if _sha256(path) != str(item["sha256"]).casefold():
                    continue
                signature = self.signature_verifier(path, str(item["publisher"]), str(item["signer_sha256"]))
                if signature.valid:
                    return AppUpdateResult(str(item["version"]), str(path), "Versão anterior verificada e pronta para restaurar.")
            except (KeyError, TypeError, ValueError, OSError):
                continue
        return AppUpdateResult(__version__, None, "Nenhuma versão anterior verificada está disponível.")

    def prepare_install(self, release: AppRelease, installer_path: str) -> None:
        rollback = self.rollback_candidate(include_current=True)
        _atomic_json(
            _pending_marker(),
            {
                "from_version": __version__,
                "to_version": release.version,
                "installer_path": installer_path,
                "rollback_installer": rollback.path or "",
                "rollback_version": rollback.version if rollback.path else "",
                "attempts": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _parse_manifest(self, payload: Any) -> AppRelease:
        if not isinstance(payload, dict) or str(payload.get("app", "")) != APP_NAME:
            raise AppUpdateError("O manifesto não pertence ao BraXYTDow.")
        if int(payload.get("schema", 0)) != 2:
            raise AppUpdateError("O canal usa um formato de manifesto incompatível.")
        channels = payload.get("channels")
        if not isinstance(channels, dict) or not isinstance(channels.get(self.channel), dict):
            raise AppUpdateError(f"O canal {self.channel} não está publicado neste manifesto.")
        item = channels[self.channel]
        version = str(item["version"]).strip().lstrip("v")
        installer_url = str(item["installer_url"]).strip()
        sha256 = str(item["sha256"]).strip().casefold()
        signer = str(item["signer_sha256"]).strip().casefold()
        publisher = str(item["publisher"]).strip()
        size = int(item["size"])
        rollout = int(item.get("rollout", 100))
        if not version or not _valid_digest(sha256) or not _valid_digest(signer):
            raise AppUpdateError("Versão, SHA-256 ou certificado de assinatura inválido no manifesto.")
        if size < 1024 * 1024 or size > MAX_INSTALLER_BYTES:
            raise AppUpdateError("O tamanho declarado do instalador está fora do limite de segurança.")
        if not publisher or not 1 <= rollout <= 100:
            raise AppUpdateError("Publicador ou percentual de liberação inválido no manifesto.")
        _require_https(installer_url)
        previous = self._parse_previous(item.get("previous"))
        return AppRelease(
            version,
            installer_url,
            sha256,
            size,
            publisher,
            signer,
            str(item.get("notes", ""))[:4000],
            self.channel,
            rollout,
            bool(item.get("mandatory", False)),
            str(item.get("published_at", ""))[:80],
            previous,
        )

    def _parse_previous(self, payload: Any) -> PreviousRelease | None:
        if not payload:
            return None
        if not isinstance(payload, dict):
            raise AppUpdateError("A referência de rollback do manifesto é inválida.")
        version = str(payload.get("version", "")).strip().lstrip("v")
        url = str(payload.get("installer_url", "")).strip()
        sha256 = str(payload.get("sha256", "")).casefold()
        signer = str(payload.get("signer_sha256", "")).casefold()
        publisher = str(payload.get("publisher", "")).strip()
        size = int(payload.get("size", 0))
        _require_https(url)
        if not version or not _valid_digest(sha256) or not _valid_digest(signer) or not publisher:
            raise AppUpdateError("A versão anterior do manifesto não possui integridade completa.")
        if size < 1024 * 1024 or size > MAX_INSTALLER_BYTES:
            raise AppUpdateError("O instalador de rollback excede o limite de segurança.")
        return PreviousRelease(version, url, sha256, size, publisher, signer)

    def _cache_previous(self, previous: PreviousRelease) -> None:
        destination = _updates_dir() / f"{APP_NAME}-Setup-{previous.version}-x64.exe"
        if destination.is_file() and destination.stat().st_size == previous.size and _sha256(destination) == previous.sha256:
            signature = self.signature_verifier(destination, previous.publisher, previous.signer_sha256)
            if signature.valid:
                self._record_verified_previous(previous, destination)
                return
        descriptor, temporary_name = tempfile.mkstemp(prefix=".rollback-", suffix=".download", dir=_updates_dir())
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            digest, received = self._download(previous.installer_url, temporary, None)
            if digest != previous.sha256 or received != previous.size:
                raise AppUpdateError("O instalador de rollback não corresponde ao manifesto.")
            signature = self.signature_verifier(temporary, previous.publisher, previous.signer_sha256)
            if not signature.valid:
                raise AppUpdateError("A assinatura do instalador de rollback não é válida.")
            os.replace(temporary, destination)
            self._record_verified_previous(previous, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _record_verified_previous(self, previous: PreviousRelease, path: Path) -> None:
        index = _read_json(_verified_index(), {"schema": 1, "installers": []})
        entries = [item for item in index.get("installers", []) if str(item.get("version")) != previous.version]
        entries.append({**asdict(previous), "path": str(path), "verified_at": datetime.now(timezone.utc).isoformat()})
        entries.sort(key=lambda item: _version_tuple(str(item.get("version", ""))), reverse=True)
        _atomic_json(_verified_index(), {"schema": 1, "installers": entries[:3]})

    def _record_verified(self, release: AppRelease, path: Path) -> None:
        index = _read_json(_verified_index(), {"schema": 1, "installers": []})
        entries = [item for item in index.get("installers", []) if str(item.get("version")) != release.version]
        entries.append({**asdict(release), "path": str(path), "verified_at": datetime.now(timezone.utc).isoformat()})
        entries.sort(key=lambda item: _version_tuple(str(item.get("version", ""))), reverse=True)
        _atomic_json(_verified_index(), {"schema": 1, "installers": entries[:3]})

    def _read(self, url: str, limit: int) -> bytes:
        _require_https(url)
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__} app-updater"})
        with urllib.request.urlopen(request, timeout=25) as response:
            _require_https(response.geturl())
            data = response.read(limit + 1)
        if len(data) > limit:
            raise AppUpdateError("O manifesto excede o limite de segurança.")
        return data

    def _download(self, url: str, destination: Path, progress: Callable[[int], None] | None) -> tuple[str, int]:
        _require_https(url)
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__} app-updater"})
        digest = hashlib.sha256()
        received = 0
        with urllib.request.urlopen(request, timeout=45) as response, destination.open("wb") as output:
            _require_https(response.geturl())
            try:
                total = int(response.headers.get("Content-Length", "0")) or None
            except ValueError:
                total = None
            if total and total > MAX_INSTALLER_BYTES:
                raise AppUpdateError("O instalador excede o limite de segurança.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_INSTALLER_BYTES:
                    raise AppUpdateError("O instalador excede o limite de segurança.")
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(min(99, int(received * 100 / total)) if total else 10)
            output.flush()
            os.fsync(output.fileno())
        if not received:
            raise AppUpdateError("O servidor retornou um instalador vazio.")
        if progress:
            progress(100)
        return digest.hexdigest(), received


def evaluate_startup_recovery(max_attempts: int = 3) -> StartupRecovery:
    marker = _read_json(_pending_marker(), {})
    if marker and str(marker.get("from_version", "")) == __version__ and int(marker.get("attempts", 0)) >= max_attempts:
        _pending_marker().unlink(missing_ok=True)
        return StartupRecovery("none", message="Rollback concluído; versão anterior restaurada.")
    if not marker or str(marker.get("to_version", "")) != __version__:
        return StartupRecovery()
    attempts = int(marker.get("attempts", 0)) + 1
    marker["attempts"] = attempts
    _atomic_json(_pending_marker(), marker)
    rollback = Path(str(marker.get("rollback_installer", "")))
    if attempts >= max_attempts and rollback.is_file():
        return StartupRecovery(
            "rollback",
            str(rollback),
            f"A versão {__version__} não concluiu a inicialização saudável após {attempts} tentativas.",
        )
    return StartupRecovery("pending", message=f"Validação pós-atualização {attempts}/{max_attempts}.")


def mark_startup_healthy() -> None:
    marker = _read_json(_pending_marker(), {})
    if marker and str(marker.get("to_version", "")) == __version__:
        _pending_marker().unlink(missing_ok=True)


def _updates_dir() -> Path:
    path = data_dir() / "app-updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _verified_index() -> Path:
    return _updates_dir() / "verified-installers.json"


def _pending_marker() -> Path:
    return _updates_dir() / "pending-update.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _require_https(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme.casefold() != "https":
        raise AppUpdateError("A atualização do aplicativo exige uma origem HTTPS.")


def _version_tuple(value: str) -> tuple[int, ...]:
    import re

    return tuple(int(item) for item in re.findall(r"\d+", value))


def _is_newer(available: str, current: str) -> bool:
    left, right = _version_tuple(available), _version_tuple(current)
    width = max(len(left), len(right), 1)
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _rollout_eligible(device_id: str, version: str, channel: str, rollout: int) -> bool:
    value = hashlib.sha256(f"{device_id}|{version}|{channel}".encode("utf-8")).digest()
    bucket = int.from_bytes(value[:4], "big") % 100 + 1
    return bucket <= max(1, min(100, rollout))


try:
    from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
except ImportError:  # pragma: no cover
    AppUpdateManager = None  # type: ignore[assignment]
else:

    class _Signals(QObject):
        result = Signal(object)
        error = Signal(str)


    class _Worker(QRunnable):
        def __init__(self, function: Callable[[], object]) -> None:
            super().__init__()
            self.function = function
            self.signals = _Signals()

        def run(self) -> None:
            try:
                self.signals.result.emit(self.function())
            except Exception as exc:  # pragma: no cover - Qt boundary
                self.signals.error.emit(str(exc))


    class AppUpdateManager(QObject):
        checked = Signal(object)
        downloaded = Signal(object)
        rollback_ready = Signal(object)
        progress = Signal(int)
        busy_changed = Signal(bool)
        failed = Signal(str)

        def __init__(self, updater: AppUpdater, parent: QObject | None = None) -> None:
            super().__init__(parent)
            self.updater = updater
            self.busy = False
            self.release: AppRelease | None = None
            self._workers: set[_Worker] = set()

        def check_async(self) -> bool:
            return self._start(self.updater.check, self._checked)

        def download_async(self) -> bool:
            if not self.release:
                return False
            return self._start(lambda: self.updater.download(self.release, self.progress.emit), self.downloaded.emit)

        def rollback_async(self) -> bool:
            return self._start(self.updater.rollback_candidate, self.rollback_ready.emit)

        def _checked(self, result: AppUpdateCheck) -> None:
            self.release = result.release if result.available else None
            self.checked.emit(result)

        def _start(self, function: Callable[[], object], success: Callable[[object], None]) -> bool:
            if self.busy:
                return False
            self.busy = True
            self.busy_changed.emit(True)
            worker = _Worker(function)
            self._workers.add(worker)
            worker.signals.result.connect(success)
            worker.signals.result.connect(lambda _value, item=worker: self._complete(item))
            worker.signals.error.connect(self.failed.emit)
            worker.signals.error.connect(lambda _value, item=worker: self._complete(item))
            QThreadPool.globalInstance().start(worker)
            return True

        def _complete(self, worker: _Worker) -> None:
            self._workers.discard(worker)
            self.busy = False
            self.busy_changed.emit(False)


__all__ = [
    "AppRelease",
    "PreviousRelease",
    "AppUpdateCheck",
    "AppUpdateError",
    "AppUpdateManager",
    "AppUpdateResult",
    "AppUpdater",
    "StartupRecovery",
    "evaluate_startup_recovery",
    "mark_startup_healthy",
]
