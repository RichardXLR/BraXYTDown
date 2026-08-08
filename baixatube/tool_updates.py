"""Safe, per-user updates for the external media tools used by BraXYTDow.

The synchronous :class:`ToolUpdater` is deliberately independent from Qt and is
easy to exercise with a fake HTTP client. :class:`ToolUpdateManager` is a small
Qt adapter that runs those blocking operations in the global thread pool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from . import __version__
from .branding import APP_NAME
from .compatibility import (
    CompatibilityCheck,
    CompatibilityLevel,
    CompatibilityReport,
    HealthProbe,
    probe_tool,
)
from .paths import (
    activate_tool,
    active_binary,
    active_tool_entry,
    bootstrap_bundled_binaries,
    executable_name,
    find_binary,
    tools_dir,
)


YT_DLP_STABLE_RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YT_DLP_NIGHTLY_RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest"
FFMPEG_RELEASE_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DENO_RELEASE_API = "https://api.github.com/repos/denoland/deno/releases/latest"
DENO_WINDOWS_ASSET = "deno-x86_64-pc-windows-msvc.zip"
ProgressCallback = Callable[[str, int, str], None]
VersionProbe = Callable[[str, Path], str | None]


class ToolUpdateError(RuntimeError):
    """Base error presented to the UI for a failed tool update."""


class IntegrityError(ToolUpdateError):
    """The downloaded artifact did not match its official digest."""


class UpdateMode(StrEnum):
    MANUAL = "manual"
    NOTIFY = "notify"
    AUTOMATIC = "automatic"


class UpdateChannel(StrEnum):
    NIGHTLY = "nightly"
    STABLE = "stable"


@dataclass(slots=True, frozen=True)
class UpdatePreferences:
    mode: UpdateMode = UpdateMode.AUTOMATIC
    channel: UpdateChannel = UpdateChannel.NIGHTLY
    check_interval_hours: int = 24
    update_yt_dlp: bool = True
    update_ffmpeg: bool = True
    update_deno: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", UpdateMode(self.mode))
        object.__setattr__(self, "channel", UpdateChannel(self.channel))
        object.__setattr__(self, "check_interval_hours", max(1, min(24 * 30, int(self.check_interval_hours))))

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "UpdatePreferences":
        values = (settings or {}).get("tool_updates", settings or {})
        if not isinstance(values, dict):
            return cls()
        automatic = values.get("auto_update_tools", True)
        try:
            mode = UpdateMode(values.get("mode", UpdateMode.AUTOMATIC if automatic else UpdateMode.MANUAL))
        except ValueError:
            mode = UpdateMode.AUTOMATIC
        try:
            channel = UpdateChannel(values.get("channel", values.get("update_channel", UpdateChannel.NIGHTLY)))
        except ValueError:
            channel = UpdateChannel.NIGHTLY
        return cls(
            mode=mode,
            channel=channel,
            check_interval_hours=values.get("check_interval_hours", 24),
            update_yt_dlp=bool(values.get("update_yt_dlp", True)),
            update_ffmpeg=bool(values.get("update_ffmpeg", True)),
            update_deno=bool(values.get("update_deno", True)),
        )

    def to_settings(self) -> dict[str, Any]:
        values = asdict(self)
        values["mode"] = self.mode.value
        values["channel"] = self.channel.value
        return {
            "auto_update_tools": self.mode == UpdateMode.AUTOMATIC,
            "update_channel": self.channel.value,
            "tool_updates": values,
        }


@dataclass(slots=True, frozen=True)
class ToolRelease:
    tool: str
    version: str
    download_url: str
    sha256: str | None
    archive: str = "executable"
    max_download_bytes: int = 256 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class ToolState:
    tool: str
    version: str | None
    path: str | None
    source: str = "missing"
    sha256: str | None = None


@dataclass(slots=True, frozen=True)
class ToolUpdate:
    tool: str
    current: ToolState
    available_version: str | None
    available: bool
    error: str = ""
    quarantined: bool = False
    quarantine_reason: str = ""
    release: ToolRelease | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True, frozen=True)
class UpdateResult:
    tool: str
    previous_version: str | None
    installed_version: str | None
    changed: bool
    path: str | None
    message: str
    error: str = ""
    validated: bool = False
    rolled_back: bool = False
    quarantined: bool = False


@dataclass(slots=True, frozen=True)
class RollbackResult:
    tool: str
    previous_version: str | None
    restored_version: str | None
    changed: bool
    path: str | None
    message: str
    error: str = ""


@dataclass(slots=True, frozen=True)
class AutoCureResult:
    reason: str
    health_before: CompatibilityReport
    updates: list[UpdateResult] = field(default_factory=list)
    rollbacks: list[RollbackResult] = field(default_factory=list)
    health_after: CompatibilityReport | None = None
    message: str = ""


@dataclass(slots=True, frozen=True)
class UpdateBatch:
    checks: list[ToolUpdate] = field(default_factory=list)
    results: list[UpdateResult] = field(default_factory=list)
    skipped_reason: str = ""


class HttpClient(Protocol):
    def get_bytes(self, url: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes: ...

    def download(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> str: ...


class UrlLibHttpClient:
    """HTTPS-only client using the platform certificate store and bounded reads."""

    def __init__(self, timeout: float = 30.0, user_agent: str = f"{APP_NAME}/{__version__} tool-updater") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    @staticmethod
    def _require_https(url: str) -> None:
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise ToolUpdateError("A atualização foi recusada porque a origem não usa HTTPS.")

    def _open(self, url: str):
        self._require_https(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/vnd.github+json, application/json, text/plain, */*",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolUpdateError(f"Não foi possível acessar o servidor de atualizações: {exc}") from exc
        self._require_https(response.geturl())
        return response

    def get_bytes(self, url: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        with self._open(url) as response:
            declared = _content_length(response.headers.get("Content-Length"))
            if declared is not None and declared > max_bytes:
                raise ToolUpdateError("A resposta do servidor excede o limite de segurança.")
            content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ToolUpdateError("A resposta do servidor excede o limite de segurança.")
        return content

    def download(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        digest = hashlib.sha256()
        with self._open(url) as response, destination.open("wb") as output:
            total = _content_length(response.headers.get("Content-Length"))
            if total is not None and total > max_bytes:
                raise ToolUpdateError("O pacote informado pelo servidor excede o limite de segurança.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise ToolUpdateError("O pacote baixado excede o limite de segurança.")
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(received, total)
            output.flush()
            os.fsync(output.fileno())
        if not received:
            raise ToolUpdateError("O servidor retornou um pacote vazio.")
        return digest.hexdigest()


class ReleaseProvider(Protocol):
    def resolve(self, tool: str) -> ToolRelease: ...


class OfficialReleaseProvider:
    """Resolve releases only from the official yt-dlp and Gyan FFmpeg feeds."""

    def __init__(self, client: HttpClient, channel: UpdateChannel = UpdateChannel.NIGHTLY) -> None:
        self.client = client
        self.channel = UpdateChannel(channel)

    def resolve(self, tool: str) -> ToolRelease:
        if tool == "yt-dlp":
            return self._yt_dlp()
        if tool == "ffmpeg":
            return self._ffmpeg()
        if tool == "deno":
            return self._deno()
        raise ValueError(f"Ferramenta desconhecida: {tool}")

    def _yt_dlp(self) -> ToolRelease:
        try:
            api_url = YT_DLP_NIGHTLY_RELEASE_API if self.channel == UpdateChannel.NIGHTLY else YT_DLP_STABLE_RELEASE_API
            release_data = json.loads(self.client.get_bytes(api_url).decode("utf-8"))
            version = str(release_data["tag_name"]).lstrip("v")
            assets = {asset.get("name"): asset for asset in release_data.get("assets", [])}
            executable = assets["yt-dlp.exe"]
            download_url = executable["browser_download_url"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ToolUpdateError("A resposta de versão do yt-dlp é inválida.") from exc

        digest_value = executable.get("digest")
        digest = _normalize_digest(digest_value)
        if not digest:
            checksum_asset = assets.get("SHA2-256SUMS")
            if not checksum_asset:
                raise ToolUpdateError("O release do yt-dlp não publicou seu SHA256.")
            checksum_text = self.client.get_bytes(checksum_asset["browser_download_url"]).decode("utf-8", "replace")
            digest = _checksum_for_file(checksum_text, "yt-dlp.exe")
        if not digest:
            raise ToolUpdateError("Não foi possível validar o SHA256 oficial do yt-dlp.")
        _require_https_url(download_url)
        return ToolRelease("yt-dlp", version, download_url, digest, "executable", 64 * 1024 * 1024)

    def _ffmpeg(self) -> ToolRelease:
        version_url = f"{FFMPEG_RELEASE_ZIP}.ver"
        checksum_url = f"{FFMPEG_RELEASE_ZIP}.sha256"
        version_text = self.client.get_bytes(version_url, max_bytes=64 * 1024).decode("utf-8", "replace")
        checksum_text = self.client.get_bytes(checksum_url, max_bytes=64 * 1024).decode("utf-8", "replace")
        version = _extract_version(version_text)
        digest = _first_digest(checksum_text)
        if not version or not digest:
            raise ToolUpdateError("A resposta de versão ou SHA256 do FFmpeg é inválida.")
        return ToolRelease("ffmpeg", version, FFMPEG_RELEASE_ZIP, digest, "zip", 192 * 1024 * 1024)

    def _deno(self) -> ToolRelease:
        try:
            release_data = json.loads(self.client.get_bytes(DENO_RELEASE_API).decode("utf-8"))
            version = str(release_data["tag_name"]).lstrip("v")
            assets = {asset.get("name"): asset for asset in release_data.get("assets", [])}
            package = assets[DENO_WINDOWS_ASSET]
            download_url = package["browser_download_url"]
            checksum_asset = assets[f"{DENO_WINDOWS_ASSET}.sha256sum"]
            checksum_url = checksum_asset["browser_download_url"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ToolUpdateError("A resposta de versão do Deno é inválida.") from exc
        checksum_text = self.client.get_bytes(checksum_url, max_bytes=64 * 1024).decode("utf-8", "replace")
        digest = _checksum_for_file(checksum_text, DENO_WINDOWS_ASSET) or _first_digest(checksum_text)
        if not digest:
            raise ToolUpdateError("Não foi possível validar o SHA256 oficial do Deno.")
        _require_https_url(download_url)
        return ToolRelease("deno", version, download_url, digest, "deno-zip", 64 * 1024 * 1024)


class ToolUpdater:
    """Synchronous updater with atomic activation and rollback-by-design."""

    def __init__(
        self,
        client: HttpClient | None = None,
        provider: ReleaseProvider | None = None,
        version_probe: VersionProbe | None = None,
        health_probe: HealthProbe | None = None,
        functional_validation: bool | None = None,
        root: Path | None = None,
        channel: UpdateChannel = UpdateChannel.NIGHTLY,
    ) -> None:
        self.client = client or UrlLibHttpClient()
        self.provider = provider or OfficialReleaseProvider(self.client, channel)
        self.version_probe = version_probe or probe_version
        self.root = root or tools_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.health_probe = health_probe or (lambda tool, path, deep: probe_tool(tool, path, deep, root=self.root))
        # Production instances always run functional validation. Tests and
        # embedders that supply a synthetic version probe may opt in explicitly.
        self.functional_validation = version_probe is None if functional_validation is None else bool(functional_validation)
        self._release_cache: dict[str, ToolRelease] = {}

    def bootstrap(self) -> list[ToolState]:
        bootstrap_bundled_binaries(root=self.root)
        return self.status()

    def status(self) -> list[ToolState]:
        return [self._state("yt-dlp"), self._state("ffmpeg"), self._state("deno")]

    def _state(self, tool: str) -> ToolState:
        managed = active_binary(tool, self.root)
        binary = str(managed) if managed else (find_binary(tool) if self.root.resolve() == tools_dir().resolve() else None)
        if not binary:
            return ToolState(tool, None, None)
        path = Path(binary)
        entry = active_tool_entry(tool, self.root)
        source = str(entry.get("source", "managed")) if entry else "system"
        digest = str(entry["sha256"]) if entry and entry.get("sha256") else None
        try:
            version = self.version_probe(tool, path)
        except (OSError, subprocess.SubprocessError):
            version = None
        return ToolState(tool, version, str(path), source, digest)

    def check(self, tools: Iterable[str] | None = None) -> list[ToolUpdate]:
        selected = _selected_tools(tools)
        states = {state.tool: state for state in self.status() if state.tool in selected}
        checks: list[ToolUpdate] = []
        for tool in selected:
            current = states.get(tool, ToolState(tool, None, None))
            try:
                release = self.provider.resolve(tool)
                self._release_cache[tool] = release
                quarantine = self._quarantine_for(release)
                checks.append(
                    ToolUpdate(
                        tool,
                        current,
                        release.version,
                        _needs_update(release, current) and quarantine is None,
                        quarantined=quarantine is not None,
                        quarantine_reason=str((quarantine or {}).get("reason", "")),
                        release=release,
                    )
                )
            except (ToolUpdateError, OSError, ValueError) as exc:
                checks.append(ToolUpdate(tool, current, None, False, str(exc)))
        # A transient outage must not postpone the next automatic attempt for
        # a full interval.  Record the cadence only when at least one provider
        # returned a valid release response.  Partial failures still count as
        # a check because the reachable tools were verified successfully.
        if any(not check.error for check in checks):
            self._record_check()
        return checks

    def update(
        self,
        tools: Iterable[str] | None = None,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
        allow_quarantined: bool = False,
    ) -> list[UpdateResult]:
        selected = _selected_tools(tools)
        try:
            with self._exclusive_update_lock():
                return self._update_unlocked(
                    selected,
                    progress,
                    force=force,
                    allow_quarantined=allow_quarantined,
                )
        except ToolUpdateError as exc:
            results: list[UpdateResult] = []
            for tool in selected:
                current = self._state(tool)
                results.append(
                    UpdateResult(
                        tool,
                        current.version,
                        current.version,
                        False,
                        current.path,
                        "A atualização não pôde ser iniciada.",
                        str(exc),
                    )
                )
            return results

    def _update_unlocked(
        self,
        selected: Iterable[str],
        progress: ProgressCallback | None,
        *,
        force: bool,
        allow_quarantined: bool,
    ) -> list[UpdateResult]:
        results: list[UpdateResult] = []
        for tool in selected:
            current = self._state(tool)
            try:
                # A forced repair must consult the provider again: extractor
                # compatibility fixes may have been published after an earlier
                # background check.  Normal check→update flows consume the
                # cached release once, preventing it from going stale forever.
                cached = self._release_cache.pop(tool, None)
                if force:
                    release = self.provider.resolve(tool)
                    self._release_cache[tool] = release
                else:
                    release = self.provider.resolve(tool) if cached is None else cached
                quarantine = self._quarantine_for(release)
                if quarantine and not allow_quarantined:
                    reason = str(quarantine.get("reason", "falha de compatibilidade"))
                    results.append(
                        UpdateResult(
                            tool,
                            current.version,
                            current.version,
                            False,
                            current.path,
                            "A versão atual foi mantida por segurança.",
                            f"A versão {release.version} está em quarentena: {reason}",
                            quarantined=True,
                        )
                    )
                    continue
                if not force and not _needs_update(release, current):
                    results.append(
                        UpdateResult(
                            tool,
                            current.version,
                            current.version,
                            False,
                            current.path,
                            "A ferramenta já está atualizada.",
                            validated=bool(current.path),
                        )
                    )
                    continue
                state = self._install(release, progress)
                results.append(
                    UpdateResult(
                        tool,
                        current.version,
                        state.version,
                        True,
                        state.path,
                        "Atualização testada, instalada e ativada com segurança.",
                        validated=True,
                    )
                )
            except (ToolUpdateError, OSError, ValueError, zipfile.BadZipFile) as exc:
                results.append(UpdateResult(tool, current.version, current.version, False, current.path, "Falha na atualização.", str(exc)))
        return results

    def repair(self, tool: str, progress: ProgressCallback | None = None) -> UpdateResult:
        """Reinstall the latest release, useful after an extractor compatibility failure."""

        return self.update((tool,), progress, force=True)[0]

    def health_check(
        self,
        tools: Iterable[str] | None = None,
        *,
        deep: bool = True,
    ) -> CompatibilityReport:
        """Exercise the active toolchain without downloading media."""

        checks: list[CompatibilityCheck] = []
        for tool in _selected_tools(tools):
            state = self._state(tool)
            if not state.path:
                checks.append(
                    CompatibilityCheck(
                        tool,
                        CompatibilityLevel.FAILED,
                        "Ferramenta não encontrada.",
                        version=state.version or "",
                    )
                )
                continue
            check = self.health_probe(tool, Path(state.path), deep and tool == "yt-dlp")
            if not check.version and state.version:
                check = CompatibilityCheck(
                    check.tool,
                    check.level,
                    check.message,
                    state.version,
                    check.detail,
                    check.duration_ms,
                )
            checks.append(check)
        report = CompatibilityReport(checks)
        self._record_report(report)
        return report

    def previous_versions(self, tool: str) -> list[ToolState]:
        """Return valid managed rollback candidates, newest first."""

        if tool not in {"yt-dlp", "ffmpeg", "deno"}:
            raise ValueError(f"Ferramenta desconhecida: {tool}")
        versions_root = self.root / "versions" / tool
        active = active_binary(tool, self.root)
        active_directory = active.parent.resolve() if active else None
        if not versions_root.is_dir():
            return []
        candidates = [child for child in versions_root.iterdir() if child.is_dir() and not child.is_symlink()]
        candidates.sort(key=lambda value: value.stat().st_mtime_ns, reverse=True)
        states: list[ToolState] = []
        for directory in candidates:
            resolved = directory.resolve()
            try:
                resolved.relative_to(versions_root.resolve())
            except ValueError:
                continue
            if active_directory and resolved == active_directory:
                continue
            binary = resolved / executable_name(tool)
            if not binary.is_file():
                continue
            if tool == "ffmpeg" and not (resolved / executable_name("ffprobe")).is_file():
                continue
            try:
                version = self.version_probe(tool, binary)
            except (OSError, subprocess.SubprocessError):
                version = None
            if version:
                states.append(ToolState(tool, version, str(binary), "rollback"))
        return states

    def rollback(self, tool: str, *, reason: str = "Rollback solicitado") -> RollbackResult:
        current = self._state(tool)
        try:
            with self._exclusive_update_lock():
                candidates = self.previous_versions(tool)
                if not candidates:
                    return RollbackResult(
                        tool,
                        current.version,
                        current.version,
                        False,
                        current.path,
                        "Não existe uma versão anterior válida.",
                        "Nenhuma cópia de rollback passou na validação.",
                    )
                restored = candidates[0]
                destination = Path(restored.path).parent  # type: ignore[arg-type]
                activate_tool(
                    tool,
                    restored.version or "rollback",
                    destination,
                    root=self.root,
                    source="rollback",
                    installed_at=datetime.now(timezone.utc).isoformat(),
                    rollback_from=current.version,
                )
                self._record_event("rollback", tool, restored.version or "", reason)
                return RollbackResult(
                    tool,
                    current.version,
                    restored.version,
                    True,
                    restored.path,
                    f"Versão {restored.version} restaurada com segurança.",
                )
        except (ToolUpdateError, OSError, ValueError) as exc:
            return RollbackResult(
                tool,
                current.version,
                current.version,
                False,
                current.path,
                "O rollback não foi concluído.",
                str(exc),
            )

    def autocure(
        self,
        reason: str,
        progress: ProgressCallback | None = None,
        *,
        tools: Iterable[str] = ("yt-dlp", "deno"),
        report_failure: bool = True,
    ) -> AutoCureResult:
        """Repair a compatibility failure and rollback repeated bad releases."""

        selected = _selected_tools(tools)
        health_before = self.health_check(selected, deep=True)
        active = self._state("yt-dlp")
        failure_count = self._record_failure("yt-dlp", active, reason) if report_failure else 0
        rollbacks: list[RollbackResult] = []
        updates: list[UpdateResult] = []

        if failure_count >= 2 and self.previous_versions("yt-dlp"):
            self._quarantine_active("yt-dlp", reason)
            rollback = self.rollback("yt-dlp", reason="AutoCura: falhas de compatibilidade repetidas")
            rollbacks.append(rollback)
        else:
            checks = self.check(selected)
            wanted = [check.tool for check in checks if check.available and not check.error]
            if wanted:
                updates = self.update(wanted, progress)
            elif report_failure or health_before.level == CompatibilityLevel.FAILED:
                # A forced refresh is intentional after a real failure: a new
                # nightly may have appeared after the periodic check.
                updates = self.update(selected, progress, force=True)

        health_after = self.health_check(selected, deep=True)
        if health_after.level == CompatibilityLevel.FAILED and not any(item.changed for item in rollbacks):
            current = self._state("yt-dlp")
            if self.previous_versions("yt-dlp"):
                self._quarantine_active("yt-dlp", health_after.checks[0].message if health_after.checks else reason)
                rollbacks.append(self.rollback("yt-dlp", reason="AutoCura: teste pós-atualização falhou"))
                health_after = self.health_check(selected, deep=True)

        if health_after.level == CompatibilityLevel.HEALTHY:
            message = "AutoCura concluído; a cadeia de download está operacional."
        elif health_after.level == CompatibilityLevel.DEGRADED:
            message = "Ferramentas válidas; o teste online ficou inconclusivo por causa da rede."
        else:
            message = "O AutoCura preservou a última versão válida, mas a compatibilidade ainda precisa de atenção."
        self._record_event("autocure", "toolchain", active.version or "", message)
        return AutoCureResult(reason, health_before, updates, rollbacks, health_after, message)

    def check_and_update(
        self,
        preferences: UpdatePreferences | None = None,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> UpdateBatch:
        preferences = preferences or UpdatePreferences()
        if isinstance(self.provider, OfficialReleaseProvider):
            self.provider.channel = preferences.channel
        if preferences.mode == UpdateMode.MANUAL and not force:
            return UpdateBatch(skipped_reason="As atualizações automáticas estão desativadas.")
        if not force and not self._check_due(preferences.check_interval_hours):
            return UpdateBatch(skipped_reason="A verificação periódica ainda não é necessária.")

        selected = []
        if preferences.update_yt_dlp:
            selected.append("yt-dlp")
        if preferences.update_ffmpeg:
            selected.append("ffmpeg")
        if preferences.update_deno:
            selected.append("deno")
        checks = self.check(selected)
        if preferences.mode != UpdateMode.AUTOMATIC:
            return UpdateBatch(checks=checks)
        wanted = [check.tool for check in checks if check.available and not check.error]
        return UpdateBatch(checks=checks, results=self.update(wanted, progress) if wanted else [])

    def _install(self, release: ToolRelease, progress: ProgressCallback | None) -> ToolState:
        _require_https_url(release.download_url)
        staging_root = self.root / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{release.tool}-", dir=staging_root) as temporary:
            temporary_path = Path(temporary)
            package = temporary_path / ("package.zip" if release.archive in {"zip", "deno-zip"} else executable_name(release.tool))

            def download_progress(received: int, total: int | None) -> None:
                percent = min(75, int(received * 75 / total)) if total else 10
                _progress(progress, release.tool, percent, "Baixando pacote oficial…")

            _progress(progress, release.tool, 1, "Conectando ao servidor oficial…")
            downloaded_digest = self.client.download(
                release.download_url,
                package,
                max_bytes=release.max_download_bytes,
                progress=download_progress,
            )
            actual_digest = _file_sha256(package)
            if downloaded_digest.lower() != actual_digest:
                raise IntegrityError("O arquivo mudou durante a gravação; a atualização foi descartada.")
            if release.sha256 and actual_digest.lower() != release.sha256.lower():
                raise IntegrityError("O SHA256 não confere com o publicado pela origem oficial.")
            _progress(progress, release.tool, 80, "Verificando e preparando os executáveis…")

            installation = temporary_path / "installation"
            installation.mkdir()
            if release.tool == "yt-dlp":
                shutil.copy2(package, installation / executable_name("yt-dlp"))
            elif release.tool == "ffmpeg" and release.archive == "zip":
                _extract_ffmpeg(package, installation)
            elif release.tool == "deno" and release.archive == "deno-zip":
                _extract_single_executable(package, installation, "deno")
            else:
                raise ToolUpdateError("Formato de pacote não reconhecido.")

            primary = installation / executable_name(release.tool)
            probed_version = self.version_probe(release.tool, primary)
            if not probed_version:
                raise ToolUpdateError("O executável baixado não iniciou corretamente; a versão anterior foi mantida.")
            if self.functional_validation:
                _progress(progress, release.tool, 88, "Executando teste funcional antes da ativação…")
                health = self.health_probe(release.tool, primary, release.tool == "yt-dlp")
                self._record_health(health, candidate_version=release.version)
                if health.level == CompatibilityLevel.FAILED:
                    self._quarantine_release(release, health.message, detail=health.detail)
                    raise ToolUpdateError(
                        f"A versão {release.version} falhou no teste funcional e foi colocada em quarentena. "
                        "A versão anterior foi mantida."
                    )

            destination = self.root / "versions" / release.tool / _safe_version_directory(release.version, actual_digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.replace(installation, destination)
            _progress(progress, release.tool, 95, "Ativando a nova versão…")
            activate_tool(
                release.tool,
                probed_version,
                destination,
                root=self.root,
                source="downloaded",
                sha256=actual_digest,
                installed_at=datetime.now(timezone.utc).isoformat(),
                release_version=release.version,
            )
            self._record_event(
                "activated",
                release.tool,
                release.version,
                "Atualização validada e ativada.",
            )
            try:
                self.prune_old_versions(release.tool, keep=3)
            except OSError:
                # Cleanup is best-effort and must never undo a valid activation.
                pass
            _progress(progress, release.tool, 100, "Atualização concluída.")
            return ToolState(release.tool, probed_version, str(destination / executable_name(release.tool)), "downloaded", actual_digest)

    def prune_old_versions(self, tool: str, *, keep: int = 3) -> list[Path]:
        """Remove old installs while preserving active and two recovery copies."""

        if tool not in {"yt-dlp", "ffmpeg", "deno"}:
            raise ValueError(f"Ferramenta desconhecida: {tool}")
        keep = max(1, int(keep))
        managed_root = self.root.resolve()
        versions_root = (managed_root / "versions" / tool).resolve()
        versions_root.relative_to(managed_root)
        if not versions_root.is_dir():
            return []

        active = active_binary(tool, self.root)
        active_directory = active.parent.resolve() if active else None
        candidates: list[Path] = []
        for child in versions_root.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            resolved = child.resolve()
            try:
                resolved.relative_to(versions_root)
            except ValueError:
                continue
            candidates.append(resolved)
        candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)

        preserved: set[Path] = {active_directory} if active_directory else set()
        for candidate in candidates:
            if len(preserved) >= keep:
                break
            preserved.add(candidate)

        removed: list[Path] = []
        for candidate in candidates:
            if candidate in preserved:
                continue
            try:
                shutil.rmtree(candidate)
            except OSError:
                # A running executable is normally locked on Windows; retain it.
                continue
            removed.append(candidate)
        return removed

    @contextmanager
    def _exclusive_update_lock(self):
        """Prevent two processes from changing the active manifest together."""

        lock_path = self.root / ".update.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ToolUpdateError("Outra instância já está atualizando as ferramentas.") from exc
            yield
        finally:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            stream.close()

    def _quarantine_key(self, tool: str, version: str, digest: str | None) -> str:
        return f"{tool}:{version}:{(digest or 'unknown')[:16]}"

    def _quarantine_for(self, release: ToolRelease) -> dict[str, Any] | None:
        state = self._read_update_state()
        item = state.get("quarantine", {}).get(self._quarantine_key(release.tool, release.version, release.sha256))
        if not isinstance(item, dict):
            return None
        try:
            until = datetime.fromisoformat(str(item.get("until", "")))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return item if until > datetime.now(timezone.utc) else None

    def _quarantine_release(
        self,
        release: ToolRelease,
        reason: str,
        *,
        detail: str = "",
        hours: int = 72,
    ) -> None:
        self._quarantine(
            release.tool,
            release.version,
            release.sha256,
            reason,
            detail=detail,
            hours=hours,
        )

    def _quarantine_active(self, tool: str, reason: str, *, hours: int = 72) -> None:
        state = self._state(tool)
        entry = active_tool_entry(tool, self.root) or {}
        version = str(entry.get("release_version") or state.version or "unknown")
        digest = str(entry.get("sha256") or state.sha256 or "") or None
        self._quarantine(tool, version, digest, reason, hours=hours)

    def _quarantine(
        self,
        tool: str,
        version: str,
        digest: str | None,
        reason: str,
        *,
        detail: str = "",
        hours: int = 72,
    ) -> None:
        state = self._read_update_state()
        key = self._quarantine_key(tool, version, digest)
        quarantine = state.setdefault("quarantine", {})
        quarantine[key] = {
            "tool": tool,
            "version": version,
            "sha256": digest or "",
            "reason": reason[:500],
            "detail": detail[-1200:],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "until": (datetime.now(timezone.utc) + timedelta(hours=max(1, hours))).isoformat(),
        }
        _write_json_atomic(self._state_path, state)
        self._record_event("quarantine", tool, version, reason)

    def _record_failure(self, tool: str, current: ToolState, reason: str) -> int:
        state = self._read_update_state()
        key = self._quarantine_key(tool, current.version or "unknown", current.sha256)
        failures = state.setdefault("failures", {})
        now = datetime.now(timezone.utc)
        previous = failures.get(key, {}) if isinstance(failures.get(key), dict) else {}
        try:
            last = datetime.fromisoformat(str(previous.get("last_at", "")))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            last = now - timedelta(days=2)
        count = int(previous.get("count", 0)) + 1 if now - last <= timedelta(hours=24) else 1
        failures[key] = {
            "tool": tool,
            "version": current.version or "",
            "sha256": current.sha256 or "",
            "count": count,
            "reason": reason[:500],
            "first_at": previous.get("first_at", now.isoformat()) if count > 1 else now.isoformat(),
            "last_at": now.isoformat(),
        }
        _write_json_atomic(self._state_path, state)
        return count

    def _record_health(self, check: CompatibilityCheck, *, candidate_version: str = "") -> None:
        state = self._read_update_state()
        state["last_candidate_health"] = {
            "tool": check.tool,
            "level": check.level.value,
            "message": check.message,
            "version": candidate_version or check.version,
            "detail": check.detail,
            "duration_ms": check.duration_ms,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(self._state_path, state)

    def _record_report(self, report: CompatibilityReport) -> None:
        state = self._read_update_state()
        state["last_health_report"] = {
            "level": report.level.value,
            "created_at": report.created_at,
            "checks": [
                {
                    "tool": check.tool,
                    "level": check.level.value,
                    "message": check.message,
                    "version": check.version,
                    "detail": check.detail,
                    "duration_ms": check.duration_ms,
                }
                for check in report.checks
            ],
        }
        _write_json_atomic(self._state_path, state)

    def _record_event(self, kind: str, tool: str, version: str, message: str) -> None:
        state = self._read_update_state()
        events = state.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            state["events"] = events
        events.append(
            {
                "kind": kind,
                "tool": tool,
                "version": version,
                "message": message[:500],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        state["events"] = events[-100:]
        _write_json_atomic(self._state_path, state)

    @property
    def _state_path(self) -> Path:
        return self.root / "update-state.json"

    def _read_update_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _record_check(self) -> None:
        state = self._read_update_state()
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(self._state_path, state)

    def _check_due(self, interval_hours: int) -> bool:
        raw = self._read_update_state().get("last_check")
        if not isinstance(raw, str):
            return True
        try:
            checked = datetime.fromisoformat(raw)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - checked >= timedelta(hours=interval_hours)


def probe_version(tool: str, path: Path) -> str | None:
    args = [str(path), "--version"] if tool in {"yt-dlp", "deno"} else [str(path), "-version"]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=12,
        check=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    if completed.returncode:
        return None
    first_line = (completed.stdout or completed.stderr).strip().splitlines()
    if not first_line:
        return None
    if tool == "yt-dlp":
        return first_line[0].strip()
    if tool == "deno":
        match = re.search(r"^deno\s+([^\s]+)", first_line[0], re.IGNORECASE)
        return match.group(1).lstrip("v") if match else None
    match = re.search(r"\bffmpeg version\s+([^\s]+)", first_line[0], re.IGNORECASE)
    return match.group(1).lstrip("n") if match else None


def _extract_ffmpeg(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for tool in ("ffmpeg", "ffprobe"):
            expected = f"{tool}.exe"
            candidates = []
            for info in archive.infolist():
                normalized = info.filename.replace("\\", "/")
                if not info.is_dir() and Path(normalized).name.lower() == expected and "/bin/" in f"/{normalized.lower()}":
                    candidates.append(info)
            if not candidates:
                raise ToolUpdateError(f"O pacote oficial não contém {expected}.")
            member = min(candidates, key=lambda item: len(item.filename))
            if member.file_size <= 0 or member.file_size > 300 * 1024 * 1024:
                raise ToolUpdateError(f"O tamanho de {expected} no pacote é inválido.")
            target = destination / executable_name(tool)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if target.stat().st_size != member.file_size:
                raise ToolUpdateError(f"A extração de {expected} ficou incompleta.")


def _extract_single_executable(archive_path: Path, destination: Path, tool: str) -> None:
    expected = f"{tool}.exe"
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [
            info
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename.replace("\\", "/")).name.lower() == expected
        ]
        if len(candidates) != 1:
            raise ToolUpdateError(f"O pacote oficial não contém um único {expected}.")
        member = candidates[0]
        if member.file_size <= 0 or member.file_size > 128 * 1024 * 1024:
            raise ToolUpdateError(f"O tamanho de {expected} no pacote é inválido.")
        target = destination / executable_name(tool)
        with archive.open(member) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        if target.stat().st_size != member.file_size:
            raise ToolUpdateError(f"A extração de {expected} ficou incompleta.")


def _selected_tools(tools: Iterable[str] | None) -> list[str]:
    # ``None`` requests the default set, while an explicitly empty iterable
    # means that the caller selected no tools.  Collapsing both falsey values
    # used to turn "update none" into "update everything".
    source = ("yt-dlp", "ffmpeg", "deno") if tools is None else tools
    selected = list(dict.fromkeys(source))
    invalid = [tool for tool in selected if tool not in {"yt-dlp", "ffmpeg", "deno"}]
    if invalid:
        raise ValueError(f"Ferramenta desconhecida: {', '.join(invalid)}")
    return selected


def _normalize_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.removeprefix("sha256:").strip().lower()
    return candidate if re.fullmatch(r"[0-9a-f]{64}", candidate) else None


def _checksum_for_file(text: str, filename: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$", line)
        if match and Path(match.group(2)).name.lower() == filename.lower():
            return match.group(1).lower()
    return None


def _first_digest(text: str) -> str | None:
    match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])", text)
    return match.group(1).lower() if match else None


def _extract_version(text: str) -> str | None:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = re.search(r"(?:version\s*)?([0-9]+(?:\.[0-9]+){1,3}(?:[-+._a-zA-Z0-9]*)?)", first)
    return match.group(1) if match else None


def _version_key(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    numbers = tuple(int(value) for value in re.findall(r"\d+", version))
    return numbers or None


def _is_newer(available: str, current: str | None) -> bool:
    if not current or current == "bundled":
        return True
    if available.strip().lower().lstrip("vn") == current.strip().lower().lstrip("vn"):
        return False
    available_key = _version_key(available)
    current_key = _version_key(current)
    if available_key and current_key:
        width = max(len(available_key), len(current_key))
        return available_key + (0,) * (width - len(available_key)) > current_key + (0,) * (width - len(current_key))
    return available != current


def _needs_update(release: ToolRelease, current: ToolState) -> bool:
    if _is_newer(release.version, current.version):
        return True
    return bool(
        current.source == "downloaded"
        and release.sha256
        and current.sha256
        and release.sha256.lower() != current.sha256.lower()
    )


def _content_length(value: str | None) -> int | None:
    try:
        result = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and result >= 0 else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_version_directory(version: str, digest: str) -> str:
    safe = "".join(character if character.isalnum() or character in ".-_" else "_" for character in version)
    return f"{safe.strip('. ') or 'unknown'}-{digest[:12]}"


def _require_https_url(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise ToolUpdateError("A origem da atualização não usa HTTPS.")


def _progress(callback: ProgressCallback | None, tool: str, percent: int, message: str) -> None:
    if callback:
        callback(tool, max(0, min(100, percent)), message)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# Qt is optional for headless tests; the core updater above has no Qt dependency.
try:  # pragma: no branch - import outcome depends on the packaging environment
    from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
except ImportError:  # pragma: no cover - exercised in installations without the UI extra
    ToolUpdateManager = None  # type: ignore[misc,assignment]
else:

    class _WorkerSignals(QObject):
        result = Signal(object)
        error = Signal(str)


    class _UpdateWorker(QRunnable):
        def __init__(self, function: Callable[[], Any]) -> None:
            super().__init__()
            self.function = function
            self.signals = _WorkerSignals()

        def run(self) -> None:
            try:
                result = self.function()
            except Exception as exc:  # protect the Qt event loop from worker failures
                try:
                    self.signals.error.emit(str(exc))
                except RuntimeError:
                    # QApplication may already be shutting down (notably during
                    # the packaged smoke test). A completed background worker
                    # must not resurrect or access a deleted Qt signal source.
                    pass
            else:
                try:
                    self.signals.result.emit(result)
                except RuntimeError:
                    pass


    class ToolUpdateManager(QObject):
        busy_changed = Signal(bool)
        started = Signal(str)
        progress = Signal(str, int, str)
        tool_checked = Signal(object)
        tool_updated = Signal(object)
        health_checked = Signal(object)
        rollback_finished = Signal(object)
        autocure_finished = Signal(object)
        finished = Signal(object)
        failed = Signal(str)

        def __init__(self, updater: ToolUpdater | None = None, parent: QObject | None = None) -> None:
            super().__init__(parent)
            self.updater = updater or ToolUpdater()
            self._busy = False
            self._workers: set[_UpdateWorker] = set()

        @property
        def busy(self) -> bool:
            return self._busy

        def bootstrap(self) -> list[ToolState]:
            return self.updater.bootstrap()

        def bootstrap_async(self) -> bool:
            """Prepare and probe managed tools outside the GUI thread."""

            return self._start("bootstrap", self.updater.bootstrap, self._bootstrapped)

        def status(self) -> list[ToolState]:
            return self.updater.status()

        def check_async(self, force: bool = False) -> bool:
            del force  # explicit checks are always performed
            return self._start("check", self.updater.check, self._checked)

        def update_async(self, tools: Iterable[str] | None = None, *, force: bool = False) -> bool:
            selected = tuple(tools) if tools is not None else None
            function = lambda: self.updater.update(selected, self._emit_progress_safely, force=force)
            return self._start("update", function, self._updated)

        def health_check_async(self, tools: Iterable[str] | None = None, *, deep: bool = True) -> bool:
            selected = tuple(tools) if tools is not None else None
            function = lambda: self.updater.health_check(selected, deep=deep)
            return self._start("health_check", function, self._health_checked)

        def rollback_async(self, tool: str, *, reason: str = "Rollback solicitado") -> bool:
            function = lambda: self.updater.rollback(tool, reason=reason)
            return self._start("rollback", function, self._rollback_finished)

        def autocure_async(
            self,
            reason: str,
            tools: Iterable[str] = ("yt-dlp", "deno"),
            *,
            report_failure: bool = True,
        ) -> bool:
            selected = tuple(tools)
            function = lambda: self.updater.autocure(
                reason,
                self._emit_progress_safely,
                tools=selected,
                report_failure=report_failure,
            )
            return self._start("autocure", function, self._autocure_finished)

        def check_and_update_async(
            self,
            preferences: UpdatePreferences | None = None,
            force: bool = False,
        ) -> bool:
            function = lambda: self.updater.check_and_update(
                preferences,
                force=force,
                progress=self._emit_progress_safely,
            )
            return self._start("check_and_update", function, self._batch_finished)

        def _emit_progress_safely(self, tool: str, percent: int, message: str) -> None:
            try:
                self.progress.emit(tool, percent, message)
            except RuntimeError:
                # The application can be closing while an HTTP read finishes.
                pass

        def _start(self, operation: str, function: Callable[[], Any], success: Callable[[Any], None]) -> bool:
            if self._busy:
                return False
            self._busy = True
            self.busy_changed.emit(True)
            self.started.emit(operation)
            worker = _UpdateWorker(function)
            self._workers.add(worker)
            worker.signals.result.connect(success)
            worker.signals.result.connect(lambda _value, item=worker: self._complete(item))
            worker.signals.error.connect(self.failed.emit)
            worker.signals.error.connect(lambda _message, item=worker: self._complete(item))
            QThreadPool.globalInstance().start(worker)
            return True

        def _complete(self, worker: _UpdateWorker) -> None:
            self._workers.discard(worker)
            self._busy = False
            self.busy_changed.emit(False)

        def _checked(self, checks: list[ToolUpdate]) -> None:
            for check in checks:
                self.tool_checked.emit(check)
            self.finished.emit(checks)

        def _bootstrapped(self, states: list[ToolState]) -> None:
            self.finished.emit(states)

        def _updated(self, results: list[UpdateResult]) -> None:
            for result in results:
                self.tool_updated.emit(result)
            self.finished.emit(results)

        def _health_checked(self, report: CompatibilityReport) -> None:
            self.health_checked.emit(report)
            self.finished.emit(report)

        def _rollback_finished(self, result: RollbackResult) -> None:
            self.rollback_finished.emit(result)
            self.finished.emit(result)

        def _autocure_finished(self, result: AutoCureResult) -> None:
            for item in result.updates:
                self.tool_updated.emit(item)
            for item in result.rollbacks:
                self.rollback_finished.emit(item)
            if result.health_after:
                self.health_checked.emit(result.health_after)
            self.autocure_finished.emit(result)
            self.finished.emit(result)

        def _batch_finished(self, batch: UpdateBatch) -> None:
            for check in batch.checks:
                self.tool_checked.emit(check)
            for result in batch.results:
                self.tool_updated.emit(result)
            self.finished.emit(batch)


__all__ = [
    "AutoCureResult",
    "CompatibilityCheck",
    "CompatibilityLevel",
    "CompatibilityReport",
    "IntegrityError",
    "OfficialReleaseProvider",
    "RollbackResult",
    "ToolRelease",
    "ToolState",
    "ToolUpdate",
    "ToolUpdateError",
    "ToolUpdateManager",
    "ToolUpdater",
    "UpdateBatch",
    "UpdateChannel",
    "UpdateMode",
    "UpdatePreferences",
    "UpdateResult",
    "UrlLibHttpClient",
    "probe_version",
]
