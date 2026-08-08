"""Functional health checks for the external media toolchain.

The updater already validates provenance and binary startup.  These probes go
one step further and exercise the work each component is expected to perform.
They intentionally never download media: the YouTube probe only extracts
metadata from a small, long-lived public video.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .paths import active_binary


YOUTUBE_CANARIES = (
    ("jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
    ("BaW_jenozKc", "https://www.youtube.com/watch?v=BaW_jenozKc"),
    ("aqz-KE-bpKQ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
)
YOUTUBE_HEALTHCHECK_ID, YOUTUBE_HEALTHCHECK_URL = YOUTUBE_CANARIES[0]
_REMOTE_EJS_ENABLED = True


class CompatibilityLevel(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class CompatibilityIssue(StrEnum):
    NONE = "none"
    NETWORK = "network"
    REGION = "region"
    REMOVED = "removed"
    RATE_LIMIT = "rate_limit"
    ACCESS = "access"
    EJS = "ejs"
    PO_TOKEN = "po_token"
    FORMAT = "format"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class CompatibilityCheck:
    tool: str
    level: CompatibilityLevel
    message: str
    version: str = ""
    detail: str = ""
    duration_ms: int = 0
    issue: CompatibilityIssue = CompatibilityIssue.NONE
    recovered_by: str = ""

    @property
    def ok(self) -> bool:
        return self.level != CompatibilityLevel.FAILED


@dataclass(slots=True, frozen=True)
class CompatibilityReport:
    checks: list[CompatibilityCheck] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def level(self) -> CompatibilityLevel:
        levels = {check.level for check in self.checks}
        if CompatibilityLevel.FAILED in levels:
            return CompatibilityLevel.FAILED
        if CompatibilityLevel.DEGRADED in levels:
            return CompatibilityLevel.DEGRADED
        return CompatibilityLevel.HEALTHY

    @property
    def ok(self) -> bool:
        return self.level != CompatibilityLevel.FAILED


HealthProbe = Callable[[str, Path, bool], CompatibilityCheck]


def probe_tool(tool: str, binary: Path, deep: bool = True, *, root: Path | None = None) -> CompatibilityCheck:
    """Run a bounded functional probe for one managed executable."""

    started = time.monotonic()
    if not binary.is_file():
        return _check(tool, CompatibilityLevel.FAILED, "Executável não encontrado.", started)
    try:
        if tool == "deno":
            completed = _run([str(binary), "eval", 'console.log("BRAXY_DENO_OK")'], timeout=15)
            success = completed.returncode == 0 and "BRAXY_DENO_OK" in completed.stdout
            return _check(
                tool,
                CompatibilityLevel.HEALTHY if success else CompatibilityLevel.FAILED,
                "Runtime JavaScript operacional." if success else "O Deno não executou o teste JavaScript.",
                started,
                detail=_tail(completed),
            )

        if tool == "ffmpeg":
            sink = "NUL" if os.name == "nt" else "/dev/null"
            completed = _run(
                [
                    str(binary),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=16x16:d=0.08",
                    "-f",
                    "null",
                    sink,
                ],
                timeout=18,
            )
            success = completed.returncode == 0
            return _check(
                tool,
                CompatibilityLevel.HEALTHY if success else CompatibilityLevel.FAILED,
                "Conversão de mídia operacional." if success else "O FFmpeg falhou no teste de conversão.",
                started,
                detail=_tail(completed),
            )

        if tool != "yt-dlp":
            return _check(tool, CompatibilityLevel.FAILED, "Ferramenta desconhecida.", started)

        version = _run([str(binary), "--version"], timeout=12)
        version_text = (version.stdout or version.stderr).strip().splitlines()
        detected = version_text[0].strip() if version.returncode == 0 and version_text else ""
        if not detected:
            return _check(tool, CompatibilityLevel.FAILED, "O yt-dlp não iniciou corretamente.", started, detail=_tail(version))
        if not deep:
            return _check(tool, CompatibilityLevel.HEALTHY, "Executável validado.", started, version=detected)

        deno = active_binary("deno", root)
        environment = _tool_environment(binary, deno, root)
        failures: list[tuple[str, CompatibilityIssue, str]] = []
        for expected_id, url in YOUTUBE_CANARIES:
            try:
                completed = _youtube_probe(binary, deno, url, expected_id, environment=environment)
            except subprocess.TimeoutExpired:
                failures.append((expected_id, CompatibilityIssue.NETWORK, "tempo limite excedido"))
                continue
            if completed.returncode == 0 and expected_id in completed.stdout:
                return _check(
                    tool,
                    CompatibilityLevel.HEALTHY,
                    f"Extração pública do YouTube operacional ({len(failures) + 1}/{len(YOUTUBE_CANARIES)} canários).",
                    started,
                    version=detected,
                )
            combined = f"{completed.stdout}\n{completed.stderr}"
            issue = classify_youtube_failure(combined)
            failures.append((expected_id, issue, _tail(completed)))
            # Only a JavaScript/challenge failure may enable remote code, and
            # only from the official yt-dlp EJS release source.
            if _REMOTE_EJS_ENABLED and issue in {CompatibilityIssue.EJS, CompatibilityIssue.PO_TOKEN, CompatibilityIssue.FORMAT} and deno:
                try:
                    fallback = _youtube_probe(
                        binary,
                        deno,
                        url,
                        expected_id,
                        environment=environment,
                        remote_ejs=True,
                    )
                except subprocess.TimeoutExpired:
                    continue
                if fallback.returncode == 0 and expected_id in fallback.stdout:
                    return _check(
                        tool,
                        CompatibilityLevel.HEALTHY,
                        "Extração recuperada com o EJS oficial sob demanda.",
                        started,
                        version=detected,
                        recovered_by="ejs:github",
                    )

        issues = {issue for _key, issue, _detail in failures}
        detail = "\n".join(f"{key}: {issue.value} · {message[-240:]}" for key, issue, message in failures)[-1200:]
        if issues and issues.issubset(
            {CompatibilityIssue.NETWORK, CompatibilityIssue.REGION, CompatibilityIssue.REMOVED, CompatibilityIssue.RATE_LIMIT, CompatibilityIssue.ACCESS}
        ):
            issue = next(iter(issues))
            return _check(
                tool,
                CompatibilityLevel.DEGRADED,
                "Binário válido; canários online inconclusivos sem evidência de quebra do extrator.",
                started,
                version=detected,
                detail=detail,
                issue=issue,
            )
        issue = CompatibilityIssue.EJS if CompatibilityIssue.EJS in issues else CompatibilityIssue.PO_TOKEN if CompatibilityIssue.PO_TOKEN in issues else CompatibilityIssue.UNKNOWN
        return _check(
            tool,
            CompatibilityLevel.FAILED,
            "O yt-dlp falhou nos canários públicos e na recuperação compatível.",
            started,
            version=detected,
            detail=detail,
            issue=issue,
        )
    except subprocess.TimeoutExpired:
        return _check(
            tool,
            CompatibilityLevel.DEGRADED if tool == "yt-dlp" else CompatibilityLevel.FAILED,
            "O teste online expirou; a ferramenta anterior será preservada." if tool == "yt-dlp" else "O teste funcional expirou.",
            started,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(tool, CompatibilityLevel.FAILED, "Não foi possível executar o teste funcional.", started, detail=str(exc))


def _run(
    args: list[str],
    *,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=environment,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _youtube_probe(
    binary: Path,
    deno: Path | None,
    url: str,
    expected_id: str,
    *,
    environment: dict[str, str],
    remote_ejs: bool = False,
) -> subprocess.CompletedProcess[str]:
    args = [
        str(binary),
        "--ignore-config",
        "--simulate",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "12",
        "--retries",
        "1",
        "--extractor-retries",
        "1",
    ]
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    if remote_ejs:
        args += ["--remote-components", "ejs:github"]
    args += ["--print", "%(id)s", url]
    return _run(args, timeout=28, environment=environment)


def _tool_environment(candidate: Path, deno: Path | None, root: Path | None) -> dict[str, str]:
    environment = dict(os.environ)
    existing_key = next((key for key in environment if key.casefold() == "path"), "PATH")
    folders = [str(candidate.parent)]
    for path in (deno, active_binary("ffmpeg", root)):
        if path and str(path.parent) not in folders:
            folders.append(str(path.parent))
    existing = environment.get(existing_key, "")
    environment[existing_key] = os.pathsep.join(folders + ([existing] if existing else []))
    return environment


def _looks_transient_network_error(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "timed out",
            "temporary failure",
            "network is unreachable",
            "unable to download webpage",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "certificate verify failed",
            "name resolution",
        )
    )


def classify_youtube_failure(value: str) -> CompatibilityIssue:
    lowered = value.casefold()
    if _looks_transient_network_error(lowered):
        return CompatibilityIssue.NETWORK
    if "not available in your country" in lowered or ("geo" in lowered and "restricted" in lowered):
        return CompatibilityIssue.REGION
    if any(marker in lowered for marker in ("video unavailable", "removed by", "does not exist")):
        return CompatibilityIssue.REMOVED
    if any(marker in lowered for marker in ("too many requests", "http error 429", "rate-limit")):
        return CompatibilityIssue.RATE_LIMIT
    if any(marker in lowered for marker in ("private video", "sign in to confirm", "not a bot", "age-restricted")):
        return CompatibilityIssue.ACCESS
    if "po token" in lowered or "pot" in lowered and "token" in lowered:
        return CompatibilityIssue.PO_TOKEN
    if any(marker in lowered for marker in ("signature extraction failed", "nsig extraction failed", "javascript runtime", "challenge solving failed", "ejs")):
        return CompatibilityIssue.EJS
    if any(marker in lowered for marker in ("requested format is not available", "no video formats")):
        return CompatibilityIssue.FORMAT
    return CompatibilityIssue.UNKNOWN


def set_remote_ejs_enabled(enabled: bool) -> None:
    global _REMOTE_EJS_ENABLED
    _REMOTE_EJS_ENABLED = bool(enabled)


def _tail(completed: subprocess.CompletedProcess[str]) -> str:
    value = (completed.stderr or completed.stdout).strip()
    return value[-800:]


def _check(
    tool: str,
    level: CompatibilityLevel,
    message: str,
    started: float,
    *,
    version: str = "",
    detail: str = "",
    issue: CompatibilityIssue = CompatibilityIssue.NONE,
    recovered_by: str = "",
) -> CompatibilityCheck:
    return CompatibilityCheck(
        tool=tool,
        level=level,
        message=message,
        version=version,
        detail=detail,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        issue=issue,
        recovered_by=recovered_by,
    )


__all__ = [
    "CompatibilityCheck",
    "CompatibilityLevel",
    "CompatibilityIssue",
    "CompatibilityReport",
    "HealthProbe",
    "YOUTUBE_HEALTHCHECK_ID",
    "YOUTUBE_HEALTHCHECK_URL",
    "YOUTUBE_CANARIES",
    "classify_youtube_failure",
    "set_remote_ejs_enabled",
    "probe_tool",
]
