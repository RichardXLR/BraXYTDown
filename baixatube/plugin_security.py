"""Quarantined installer for optional yt-dlp PO Token provider plugins."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .compatibility import YOUTUBE_HEALTHCHECK_URL
from .paths import data_dir, find_binary


ALLOWED_PROVIDERS = {
    "bgutil-ytdlp-pot-provider": "Brainicism/bgutil-ytdlp-pot-provider",
    "yt-dlp-getpot-wpc": "coletdjnz/yt-dlp-getpot-wpc",
}
MAX_PLUGIN_BYTES = 48 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024


class PluginSecurityError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class PluginState:
    name: str = ""
    version: str = ""
    path: str = ""
    sha256: str = ""
    active: bool = False
    message: str = "Nenhum provedor ativado."


class PoTokenPluginManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (data_dir() / "po-token-provider")
        self.root.mkdir(parents=True, exist_ok=True)

    def state(self) -> PluginState:
        manifest = _read_json(self.root / "active.json", {})
        path = Path(str(manifest.get("path", "")))
        if path.is_dir() and _inside(path, self.root / "active"):
            return PluginState(
                str(manifest.get("name", "")),
                str(manifest.get("version", "")),
                str(path),
                str(manifest.get("sha256", "")),
                True,
                "Provedor verificado e isolado no processo do yt-dlp.",
            )
        return PluginState()

    def install(self, manifest_url: str) -> PluginState:
        _require_https(manifest_url)
        manifest = json.loads(_read_url(manifest_url, 256 * 1024).decode("utf-8"))
        name, version, archive_url, expected_sha, marker = self._validate_manifest(manifest)
        quarantine_root = self.root / "quarantine"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        quarantine = Path(tempfile.mkdtemp(prefix="candidate-", dir=quarantine_root))
        archive = quarantine / "provider.zip"
        extracted = quarantine / "plugin"
        try:
            digest = _download(archive_url, archive)
            if digest != expected_sha:
                raise PluginSecurityError("O SHA-256 do plugin não confere com a lista permitida.")
            _safe_extract(archive, extracted)
            self._probe(extracted, marker)
            active_root = self.root / "active"
            active_root.mkdir(parents=True, exist_ok=True)
            destination = active_root / f"{_safe_name(name)}-{_safe_name(version)}"
            staged = active_root / f".{destination.name}.staged"
            if staged.exists():
                shutil.rmtree(staged)
            shutil.copytree(extracted, staged)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(staged, destination)
            _atomic_json(
                self.root / "active.json",
                {"schema": 1, "name": name, "version": version, "path": str(destination), "sha256": digest},
            )
            return self.state()
        finally:
            shutil.rmtree(quarantine, ignore_errors=True)

    def disable(self) -> PluginState:
        (self.root / "active.json").unlink(missing_ok=True)
        return PluginState(message="Provedor desativado; arquivos verificados foram preservados para diagnóstico.")

    def _validate_manifest(self, payload: Any) -> tuple[str, str, str, str, str]:
        if not isinstance(payload, dict) or int(payload.get("schema", 0)) != 1:
            raise PluginSecurityError("Manifesto de plugin incompatível.")
        name = str(payload.get("name", ""))
        if name not in ALLOWED_PROVIDERS:
            raise PluginSecurityError("O provedor não pertence à lista permitida do BraXYTDow.")
        version = str(payload.get("version", "")).strip()
        archive_url = str(payload.get("archive_url", "")).strip()
        sha256 = str(payload.get("sha256", "")).casefold()
        marker = str(payload.get("probe_marker", name.split("-")[0]))[:80]
        _require_https(archive_url)
        parsed_archive = urllib.parse.urlsplit(archive_url)
        repository = ALLOWED_PROVIDERS[name].casefold()
        if parsed_archive.hostname != "github.com" or f"/{repository}/releases/download/" not in parsed_archive.path.casefold():
            raise PluginSecurityError("O arquivo do plugin não vem do repositório permitido.")
        if not version or len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise PluginSecurityError("Versão ou hash obrigatório ausente no manifesto do plugin.")
        return name, version, archive_url, sha256, marker

    def _probe(self, plugin_dir: Path, marker: str) -> None:
        binary = find_binary("yt-dlp")
        if not binary:
            raise PluginSecurityError("yt-dlp não encontrado para testar o provedor em quarentena.")
        completed = subprocess.run(
            [
                binary,
                "--ignore-config",
                "--plugin-dirs",
                str(plugin_dir),
                "--verbose",
                "--simulate",
                "--no-playlist",
                YOUTUBE_HEALTHCHECK_URL,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=55,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        combined = f"{completed.stdout}\n{completed.stderr}"
        if completed.returncode or marker.casefold() not in combined.casefold():
            raise PluginSecurityError("O provedor falhou no teste isolado e permaneceu em quarentena.")


def active_plugin_dir(enabled: bool = True, root: Path | None = None) -> Path | None:
    if not enabled:
        return None
    state = PoTokenPluginManager(root).state()
    return Path(state.path) if state.active else None


def _safe_extract(archive: Path, destination: Path) -> None:
    total = 0
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        for member in members:
            pure = PurePosixPath(member.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise PluginSecurityError("O plugin contém um caminho inseguro.")
            if member.external_attr >> 16 & 0o170000 == 0o120000:
                raise PluginSecurityError("Links simbólicos não são permitidos no plugin.")
            total += member.file_size
            if total > MAX_EXPANDED_BYTES:
                raise PluginSecurityError("O plugin excede o limite após descompactação.")
        if not any("yt_dlp_plugins" in PurePosixPath(item.filename.replace("\\", "/")).parts for item in members):
            raise PluginSecurityError("O pacote não contém a estrutura oficial yt_dlp_plugins.")
        package.extractall(destination)


def _download(url: str, destination: Path) -> str:
    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(url, headers={"User-Agent": "BraXYTDow plugin-quarantine"})
    with urllib.request.urlopen(request, timeout=40) as response, destination.open("wb") as output:
        _require_https(response.geturl())
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_PLUGIN_BYTES:
                raise PluginSecurityError("O plugin excede o limite de download.")
            output.write(chunk)
            digest.update(chunk)
    if not received:
        raise PluginSecurityError("O servidor retornou um plugin vazio.")
    return digest.hexdigest()


def _read_url(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "BraXYTDow plugin-manifest"})
    with urllib.request.urlopen(request, timeout=30) as response:
        _require_https(response.geturl())
        data = response.read(limit + 1)
    if len(data) > limit:
        raise PluginSecurityError("O manifesto do plugin excede o limite.")
    return data


def _require_https(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme.casefold() != "https":
        raise PluginSecurityError("Plugins só podem ser obtidos por HTTPS.")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_name(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in "-_.")[:80] or "provider"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


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


try:
    from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
except ImportError:  # pragma: no cover
    PoTokenPluginController = None  # type: ignore[assignment]
else:

    class _PluginSignals(QObject):
        result = Signal(object)
        error = Signal(str)


    class _PluginWorker(QRunnable):
        def __init__(self, function) -> None:
            super().__init__()
            self.function = function
            self.signals = _PluginSignals()

        def run(self) -> None:
            try:
                self.signals.result.emit(self.function())
            except Exception as exc:  # pragma: no cover - Qt boundary
                self.signals.error.emit(str(exc))


    class PoTokenPluginController(QObject):
        finished = Signal(object)
        failed = Signal(str)
        busy_changed = Signal(bool)

        def __init__(self, manager: PoTokenPluginManager | None = None, parent: QObject | None = None) -> None:
            super().__init__(parent)
            self.manager = manager or PoTokenPluginManager()
            self.busy = False
            self._worker: _PluginWorker | None = None

        def install_async(self, manifest_url: str) -> bool:
            if self.busy:
                return False
            self.busy = True
            self.busy_changed.emit(True)
            worker = _PluginWorker(lambda: self.manager.install(manifest_url))
            self._worker = worker
            worker.signals.result.connect(self.finished.emit)
            worker.signals.error.connect(self.failed.emit)
            worker.signals.result.connect(lambda _value: self._complete())
            worker.signals.error.connect(lambda _value: self._complete())
            QThreadPool.globalInstance().start(worker)
            return True

        def _complete(self) -> None:
            self._worker = None
            self.busy = False
            self.busy_changed.emit(False)


__all__ = [
    "ALLOWED_PROVIDERS",
    "PluginSecurityError",
    "PluginState",
    "PoTokenPluginController",
    "PoTokenPluginManager",
    "active_plugin_dir",
]
