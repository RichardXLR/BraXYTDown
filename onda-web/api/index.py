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

from anyio import CancelScope
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .engine import FORMATS, VIDEO_FORMATS, MediaSettings, SessionOptions, VIDEO_DURATION, LOSSLESS, MAX_DURATION, LOSSLESS_DURATION, inspect_media, prepare_download, runtime_options, source_for
from .security import AudioError, Guard, public_url
from .cookies import MAX_COOKIE_BYTES, validate_session
from .auth import register_auth, auth_capabilities
from .account import register_account

app = FastAPI(title="Onda API", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
SLOTS = threading.BoundedSemaphore(2)
LOGGER = logging.getLogger("onda")


class LinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=4096)
    cookies: str | None = Field(default=None, max_length=MAX_COOKIE_BYTES, repr=False)
    video_password: str | None = Field(default=None, max_length=256, strict=True, repr=False)
    user_agent: str | None = Field(default=None, max_length=512, strict=True, repr=False)
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

    @field_validator("video_password")
    @classmethod
    def validate_video_password(cls, value):
        return SessionOptions(video_password=value).video_password

    @field_validator("user_agent")
    @classmethod
    def validate_user_agent(cls, value):
        return SessionOptions(user_agent=value).user_agent

    def session_options(self):
        if not self.video_password and not self.user_agent:
            return None
        return SessionOptions(video_password=self.video_password, user_agent=self.user_agent)


class SessionValidationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=4096)
    cookies: str = Field(min_length=1, max_length=MAX_COOKIE_BYTES, repr=False)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return LinkInput.validate_url(value)

    @field_validator("cookies")
    @classmethod
    def validate_cookie_bytes(cls, value):
        return LinkInput.validate_cookie_bytes(value)


class DownloadInput(LinkInput):
    format: Literal["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff", "mp4", "webm", "mkv", "mov"] = "mp3"
    quality: Literal[128, 192, 256, 320, "source"] = 192
    video_resolution: Literal["source", "2160", "1440", "1080", "720", "480", "360"] = "source"
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
    payload = {"error": exc.message, "code": exc.code}
    attempts = getattr(exc, "recovery_attempts", None)
    if isinstance(attempts, int) and 1 <= attempts <= 3:
        payload["recovery"] = {"attempts": attempts, "exhausted": bool(getattr(exc, "recovery_exhausted", False))}
    return JSONResponse(payload, status_code=exc.status,
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
            "videoResolutions": ["source", "2160", "1440", "1080", "720", "480", "360"],
            "maxLosslessDuration": LOSSLESS_DURATION, "maxSourceMB": 128, "maxOutputMB": 100,
            "durationLimited": False, "operationTimeoutSeconds": 240,
            "downloadRecovery": {"enabled": True, "maxAttempts": 3, "resume": True,
                                 "nativeAlternatives": True, "requiresCompleteFile": True},
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
                guard.error = guard.error or AudioError("A origem demorou demais. Tente um vídeo menor ou outro link.", "timeout", 504)
                guard.abort()
        return await asyncio.shield(task)
    except BaseException:
        guard.abort()
        # Network sockets have an 8 s timeout and FFmpeg is checked every 100 ms.
        await settle_worker(task)
        raise
    finally:
        guard.close_sockets()


async def settle_worker(task: asyncio.Task):
    """A second cancellation must never abandon a worker using local files.

    AnyIO middleware can repeatedly cancel each await within its cancel scope.
    Shield that scope and also absorb explicit asyncio Task.cancel() calls until
    the already-aborted worker has exited. The caller then re-raises its original
    exception; retrieving the worker result prevents unhandled-task warnings.
    """
    with CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException:
            pass


class DownloadResponse(StreamingResponse):
    """Own the iterator and temporary files for the complete ASGI lifecycle."""

    def __init__(self, content, *, cleanup, **kwargs):
        super().__init__(content, **kwargs)
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A failed header send happens before the generator ever starts.
            # A failed body send can instead leave it suspended with an open
            # file. Close it explicitly, then run the idempotent cleanup even
            # when no background task or generator-finally was reached.
            with CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    self.cleanup()


def acquire_slot():
    if not SLOTS.acquire(blocking=False):
        raise AudioError("Estamos processando outras mídias. Aguarde alguns segundos e tente novamente.", "busy", 429)


async def acquire_download_slot(request: Request):
    for attempt in range(1, 4):
        try:
            acquire_slot()
            return attempt > 1
        except AudioError as exc:
            if exc.code != "busy" or attempt == 3:
                raise
            await asyncio.sleep(.3 if attempt == 1 else .7)
            if await request.is_disconnected():
                raise AudioError("Download cancelado.", "cancelled", 499) from exc


@app.post("/api/inspect")
async def inspect_link(body: LinkInput, request: Request):
    acquire_slot()
    guard = Guard(seconds=40, maximum_bytes=16 * 1024 * 1024)
    try:
        # Adapt the shared runner's final guard argument to metadata extraction.
        session = body.session_options()
        options = {"cookies": body.cookies, "media_type": body.media_type}
        if session is not None:
            options["session"] = session
        return await run_guarded(request, guard, functools.partial(inspect_media, **options), body.url)
    finally:
        SLOTS.release()


@app.post("/api/session/validate")
async def validate_imported_session(body: SessionValidationInput):
    source = source_for(body.url)
    return validate_session(body.cookies, body.url, direct_media=source == "Arquivo direto")


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
    queued = await acquire_download_slot(request)
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
        options = {"cookies": body.cookies, "settings": body.settings()}
        session = body.session_options()
        if session is not None:
            options["session"] = session
        target, details = await run_guarded(request, guard, functools.partial(prepare_download, **options),
                                           body.url, directory, body.format, body.quality)
        headers = download_headers(details["title"], body.format, target.stat().st_size)
        recovery = details.get("recovery", {})
        attempts = recovery.get("attempts", 1)
        if isinstance(attempts, int) and 1 <= attempts <= 3:
            headers["X-Recovery-Attempts"] = str(attempts)
        headers["X-Recovery-Resumed"] = "1" if recovery.get("resumed") is True else "0"
        headers["X-Recovery-Conversion"] = "1" if recovery.get("conversion_recovered") is True else "0"
        headers["X-Recovery-Queued"] = "1" if queued else "0"
        resolution = details.get("output_resolution")
        if isinstance(resolution, int) and 0 < resolution <= 2160:
            headers["X-Media-Resolution"] = str(resolution)

        async def stream_file():
            try:
                with target.open("rb") as file:
                    while chunk := file.read(64 * 1024):
                        yield chunk
                        await asyncio.sleep(0)
            finally:
                cleanup()

        mime = VIDEO_FORMATS[body.format][2] if body.media_type == "video" else FORMATS[body.format][1]
        return DownloadResponse(stream_file(), cleanup=cleanup,
                                media_type=mime, headers=headers)
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
