"""Compatibility information and explicit, bounded online checks.

Releases contain their entire toolchain. No request can replace the running
executables or promote a deployment. Administrative release automation lives
in the repository's AutoCura workflow.
"""
from __future__ import annotations

import functools
import asyncio
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.parse

import imageio_ffmpeg
from pydantic import Field
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.version import __version__ as ytdlp_version

from . import engine
from .security import AudioError, Guard

ROOT = Path(__file__).resolve().parents[1]
VERSION_LOCK = threading.Lock()


@functools.lru_cache(maxsize=1)
def _version_data():
    """Only trusted executables and package data; never a user supplied path."""
    result = {"ytDlp": ytdlp_version, "ffmpeg": "indisponível", "deno": "indisponível"}
    try:
        result["ffmpeg"] = imageio_ffmpeg.get_ffmpeg_version()
    except (OSError, RuntimeError):
        pass
    runtime = engine.runtime_options().get("deno", {}).get("path")
    if runtime:
        try:
            output = subprocess.check_output([runtime, "--version"], timeout=5, text=True,
                                             stderr=subprocess.DEVNULL)
            result["deno"] = output.splitlines()[0].split()[1]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return result


def versions():
    with VERSION_LOCK:
        return dict(_version_data())


@functools.lru_cache(maxsize=1)
def extractor_count():
    return sum(1 for item in gen_extractor_classes()
               if item.IE_NAME != "generic" and not item.IE_NAME.startswith(":")
               and item.working())


@functools.lru_cache(maxsize=1)
def provider_classes():
    return {item.ie_key(): item for item in gen_extractor_classes()
            if item.IE_NAME != "generic" and not item.IE_NAME.startswith(":") and item.working()}


def provider_catalog():
    return [{"id": "DirectMedia", "name": "Arquivo direto de áudio ou vídeo"}] + sorted(
        [{"id": key, "name": str(item.IE_NAME)} for key, item in provider_classes().items()],
        key=lambda item: item["name"].casefold())


def validate_provider(provider, url):
    if not provider:
        return
    if provider == "DirectMedia":
        matches = Path(urllib.parse.urlsplit(url).path.lower()).suffix in engine.DIRECT_EXTENSIONS
    else:
        selected = provider_classes().get(provider)
        matches = bool(selected and selected.suitable(url))
    if not matches:
        raise AudioError("O link precisa corresponder à plataforma selecionada. Escolha detectar automaticamente para outra fonte.", "provider_mismatch", 422)


def release_status():
    fallback = {"mode": "deployment", "automated": False, "snapshot_only": True, "state": "not_configured",
                "message": "Atualizações usam um pacote completo testado antes da publicação. "
                           "A rotina automática depende da conexão do repositório e das credenciais do mantenedor."}
    try:
        path = ROOT / "autocura-report.json"
        if not path.is_file():
            path = ROOT / "public" / "autocura.json"
        report = json.loads(path.read_text())
        if not isinstance(report, dict):
            return fallback
        # This is a build snapshot, not live deployment or quarantine history.
        config = report.get("selfHealing", report)
        if isinstance(config, dict):
            aliases = {"strategy": "mode", "status": "state"}
            for source, target in aliases.items():
                if source in config:
                    fallback[target] = config[source]
            for key in ("mode", "state", "message"):
                if key in config:
                    fallback[key] = config[key]
            # Only /api/maintenance can confirm a current workflow execution.
            # A persisted build report remains a snapshot even after activation.
            fallback["snapshot_automation_enabled"] = config.get("automation_enabled") is True
    except (OSError, ValueError, TypeError):
        pass
    return fallback


def register(app):
    # The entry point owns the worker runner, input schema and shared quota.
    from .index import LinkInput, acquire_slot, run_guarded, SLOTS

    class CompatibilityInput(LinkInput):
        provider: str | None = Field(default=None, max_length=128)

    globals()["CompatibilityInput"] = CompatibilityInput

    @app.get("/api/compatibility/providers")
    async def providers():
        return {"version": ytdlp_version, "providers": provider_catalog()}

    @app.get("/api/compatibility")
    async def compatibility():
        current = await asyncio.to_thread(versions)
        return {
            "status": "available" if current["ffmpeg"] != "indisponível" else "degraded",
            "versions": current, "sitesCount": extractor_count(),
            "selfHealing": release_status(),
            "canaries": [
                {"name": "Áudio de teste gerado pelo projeto", "url": "/canary.wav"},
                {"name": "YouTube · verificação de acesso", "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
            ],
            "limits": {"maxDuration": engine.MAX_DURATION,
                       "maxLosslessDuration": engine.LOSSLESS_DURATION,
                       "maxVideoDuration": engine.VIDEO_DURATION,
                       "maxSourceMB": engine.MAX_SOURCE // 1024 // 1024,
                       "operationTimeoutSeconds": 240,
                       "maxOutputMB": engine.MAX_OUTPUT // 1024 // 1024},
            "formats": {"audio": list(engine.FORMATS), "video": list(engine.VIDEO_FORMATS)},
            "testScope": "metadata",
        }

    # Local imports cannot be used in FastAPI's postponed annotation lookup.
    from fastapi import Request
    globals()["LinkInput"] = LinkInput
    globals()["Request"] = Request

    @app.post("/api/compatibility/test")
    async def test_link(body: CompatibilityInput, request: Request):
        acquire_slot()
        started = time.monotonic()
        guard = Guard(seconds=40, maximum_bytes=16 * 1024 * 1024)
        try:
            validate_provider(body.provider, body.url)
            inspect = engine.inspect_media
            options = {}
            if getattr(body, "cookies", None):
                options["cookies"] = body.cookies
            if body.media_type != "audio":
                options["media_type"] = body.media_type
            session = body.session_options()
            if session is not None:
                options["session"] = session
            if options:
                inspect = functools.partial(inspect, **options)
            details = await run_guarded(request, guard, inspect, body.url)
            return {"ok": True, "status": "passed", "details": details,
                    "scope": "metadata", "elapsedMs": round((time.monotonic() - started) * 1000)}
        except AudioError as exc:
            status = "blocked" if exc.code in ("platform_blocked", "busy", "timeout", "upstream_timeout", "upstream_error") else "failed"
            return {"ok": False, "status": status, "error": exc.message, "code": exc.code,
                    "scope": "metadata", "elapsedMs": round((time.monotonic() - started) * 1000)}
        finally:
            SLOTS.release()
