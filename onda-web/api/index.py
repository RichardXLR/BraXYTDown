"""FastAPI entrypoint for Vercel's single Python function."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import urllib.parse
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.background import BackgroundTask

from .engine import FORMATS, VIDEO_FORMATS, MediaSettings, VIDEO_DURATION, LOSSLESS, MAX_DURATION, LOSSLESS_DURATION, inspect_media, prepare_download, runtime_options
from .security import AudioError, Guard, public_url
from .cookies import MAX_COOKIE_BYTES
from .auth import register_auth, auth_capabilities
from .account import register_account

app = FastAPI(title="Onda API", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
SLOTS = threading.BoundedSemaphore(2)
LOGGER = logging.getLogger("onda")


class LinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=4096)
    cookies: str | None = Field(default=None, max_length=MAX_COOKIE_BYTES, repr=False)
    media_type: Literal["audio", "video"] = "audio"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        value = value.strip()
        public_url(value, resolve=False)
        return value

    @field_validator("cookies")
    @classmethod
    def validate_cookie_bytes(cls, value):
        if value is not None and len(value.encode("utf-8")) > MAX_COOKIE_BYTES:
            raise AudioError("O arquivo de cookies deve ter no máximo 64 KB.", "invalid_cookies", 422)
        return value


class DownloadInput(LinkInput):
    format: Literal["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff", "mp4", "webm", "mkv", "mov"] = "mp3"
    quality: Literal[128, 192, 256, 320, "source"] = 192
    video_resolution: Literal["source", "1080", "720", "480", "360"] = "source"
    trim_start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    trim_end: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    strip_metadata: bool = Field(default=True, strict=True)
    mute: bool = Field(default=False, strict=True)
    normalize_audio: bool = Field(default=False, strict=True)

    @field_validator("quality", mode="before")
    @classmethod
    def normalize_quality(cls, value):
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

    @field_validator("video_resolution", mode="before")
    @classmethod
    def normalize_resolution(cls, value):
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @model_validator(mode="after")
    def media_options(self):
        if self.media_type == "video" and "format" not in self.model_fields_set:
            self.format = "mp4"
        allowed = VIDEO_FORMATS if self.media_type == "video" else FORMATS
        if self.format not in allowed:
            raise ValueError("Escolha um formato compatível com o tipo de mídia.")
        if self.quality == "source" and self.media_type == "audio" and self.format not in LOSSLESS:
            raise ValueError("Escolha uma taxa de bits para esse formato.")
        if self.media_type == "audio" and self.mute:
            raise ValueError("Silenciar está disponível apenas para vídeo.")
        if self.mute and self.normalize_audio:
            raise ValueError("Um vídeo sem áudio não pode normalizar volume.")
        if self.trim_end is not None and self.trim_end <= (self.trim_start or 0):
            raise ValueError("O fim do corte deve ficar depois do início.")
        return self

    def settings(self):
        return MediaSettings(**{name: getattr(self, name) for name in MediaSettings.__dataclass_fields__})


@app.exception_handler(AudioError)
async def audio_error(_request, exc):
    LOGGER.warning("audio_request_failed code=%s status=%d", exc.code, exc.status)
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status,
                        headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _exc):
    return JSONResponse({"error": "Confira o link, o formato e a qualidade selecionados.", "code": "invalid_request"}, status_code=422)


@app.middleware("http")
async def default_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health():
    deployment_id = os.environ.get("VERCEL_DEPLOYMENT_ID", "")
    if not re.fullmatch(r"dpl_[A-Za-z0-9]{1,100}", deployment_id):
        deployment_id = None
    return {"ok": True, "formats": list(FORMATS), "maxDuration": MAX_DURATION,
            "videoFormats": list(VIDEO_FORMATS), "maxVideoDuration": VIDEO_DURATION,
            "videoResolutions": ["source", "1080", "720", "480", "360"],
            "maxLosslessDuration": LOSSLESS_DURATION, "maxSourceMB": 128, "maxOutputMB": 100,
            "durationLimited": False, "operationTimeoutSeconds": 240,
            "auth": auth_capabilities(),
            "deploymentId": deployment_id,
            "jsRuntime": next(iter(runtime_options()), None)}


async def run_guarded(request: Request, guard: Guard, function, *args):
    """Wait for workers to exit before deleting their files, including disconnects."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, guard))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=.25)
            if done:
                break
            if await request.is_disconnected():
                guard.abort()
            if guard.remaining <= 0:
                guard.error = AudioError("A origem demorou demais. Tente um vídeo menor ou outro link.", "timeout", 504)
                guard.abort()
        return await asyncio.shield(task)
    except BaseException:
        guard.abort()
        # Network sockets have an 8 s timeout and FFmpeg is checked every 100 ms.
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise
    finally:
        guard.close_sockets()


def acquire_slot():
    if not SLOTS.acquire(blocking=False):
        raise AudioError("Estamos processando outras mídias. Aguarde alguns segundos e tente novamente.", "busy", 429)


@app.post("/api/inspect")
async def inspect_link(body: LinkInput, request: Request):
    acquire_slot()
    guard = Guard(seconds=40, maximum_bytes=16 * 1024 * 1024)
    try:
        # Adapt the shared runner's final guard argument to metadata extraction.
        return await run_guarded(request, guard, functools.partial(inspect_media, cookies=body.cookies, media_type=body.media_type), body.url)
    finally:
        SLOTS.release()


def download_headers(title: str, audio_format: str, size: int):
    clean_title = re.sub(r"[\x00-\x1f\x7f/\\]", "", title).strip()[:160] or "audio"
    ascii_title = re.sub(r"[^A-Za-z0-9 ._-]", "", clean_title).strip() or "audio"
    encoded = urllib.parse.quote(f"{clean_title}.{audio_format}", safe="")
    return {"Content-Disposition": f'attachment; filename="{ascii_title}.{audio_format}"; filename*=UTF-8\'\'{encoded}',
            "Content-Length": str(size), "X-Audio-Title": urllib.parse.quote(clean_title, safe=""),
            "X-Media-Title": urllib.parse.quote(clean_title, safe=""),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@app.post("/api/download")
async def download_link(body: DownloadInput, request: Request):
    acquire_slot()
    try:
        directory = Path(tempfile.mkdtemp(prefix="onda-", dir="/tmp"))
    except OSError as exc:
        SLOTS.release()
        raise AudioError("O servidor está sem espaço temporário. Aguarde e tente novamente.", "storage_busy", 503) from exc
    guard = Guard(seconds=240)
    cleanup_lock = threading.Lock()
    cleaned = False

    def cleanup():
        nonlocal cleaned
        with cleanup_lock:
            if not cleaned:
                shutil.rmtree(directory, ignore_errors=True)
                SLOTS.release()
                cleaned = True

    try:
        target, details = await run_guarded(request, guard, functools.partial(prepare_download, cookies=body.cookies, settings=body.settings()),
                                           body.url, directory, body.format, body.quality)
        headers = download_headers(details["title"], body.format, target.stat().st_size)

        async def stream_file():
            try:
                with target.open("rb") as file:
                    while chunk := file.read(64 * 1024):
                        yield chunk
                        await asyncio.sleep(0)
            finally:
                cleanup()

        mime = VIDEO_FORMATS[body.format][2] if body.media_type == "video" else FORMATS[body.format][1]
        return StreamingResponse(stream_file(), media_type=mime,
                                 headers=headers, background=BackgroundTask(cleanup))
    except BaseException:
        cleanup()
        raise


from .compatibility import register
from .player import register as register_player
from .maintenance import register as register_maintenance

register(app)
register_player(app)
register_maintenance(app)
register_account(app)
register_auth(app)

# Vercel serves public/ from its CDN. This fallback is only for local uvicorn.
public = Path(__file__).resolve().parents[1] / "public"
if not os.environ.get("VERCEL") and public.exists():
    app.mount("/", StaticFiles(directory=public, html=True), name="web")
