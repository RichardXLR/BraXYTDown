"""Safe source previews: official embeds or publicly readable progressive media.

Extractors support more sites than browsers can embed. Never invent an iframe for
an unknown host, expose a session header, or describe an adaptive stream as a
playable single file. Preview descriptors are deliberately ephemeral.
"""
from __future__ import annotations

import functools
import re
import urllib.parse
from pathlib import Path

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from yt_dlp.networking import Request as MediaRequest

from . import engine
from .cookies import MAX_COOKIE_BYTES, parse_netscape
from .security import AudioError, Guard, public_url

FRAME_HOSTS = frozenset({
    "www.youtube-nocookie.com", "www.tiktok.com", "player.vimeo.com",
    "www.dailymotion.com", "player.twitch.tv", "clips.twitch.tv",
    "www.facebook.com", "www.instagram.com", "player.bilibili.com",
})
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mov"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac"}
UNAVAILABLE = ("Esta fonte não oferece uma prévia compatível neste navegador. "
               "Você pode abrir o conteúdo na plataforma e testar o download separadamente.")


class PlayerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=4096)
    cookies: str | None = Field(default=None, max_length=MAX_COOKIE_BYTES, repr=False)

    @field_validator("url")
    @classmethod
    def valid_link(cls, value):
        value = value.strip()
        public_url(value, resolve=False)
        return value

    @field_validator("cookies")
    @classmethod
    def bounded_cookies(cls, value):
        if value is not None and len(value.encode("utf-8")) > MAX_COOKIE_BYTES:
            raise AudioError("O arquivo de cookies deve ter no máximo 64 KB.", "invalid_cookies", 422)
        return value


def _family(host, domain):
    return host == domain or host.endswith("." + domain)


def _parent(host):
    # Twitch requires the actual embedding hostname. This value contains no
    # scheme, path or port, and is encoded as a query value, never HTML.
    host = (host or "").lower().rstrip(".")
    if host in {"localhost", "127.0.0.1"}:
        return host
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
        try:
            public_url("https://" + host, resolve=False)
            return host
        except AudioError:
            pass
    return "onda-audio.vercel.app"


def official_embed(url: str, parent_host: str = "onda-audio.vercel.app") -> dict | None:
    host, _ = public_url(url, resolve=False)
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/")
    query = urllib.parse.parse_qs(parsed.query)
    identifier = None
    provider = None
    source = None
    vertical = False

    if _family(host, "youtube.com") or _family(host, "youtube-nocookie.com") or host in {"youtu.be", "www.youtu.be"}:
        if host in {"youtu.be", "www.youtu.be"}:
            identifier = path.removeprefix("/")
        elif path == "/watch":
            identifier = query.get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(shorts|live|embed)/([A-Za-z0-9_-]{11})", path)
            if match:
                identifier, vertical = match[2], match[1] == "shorts"
        if identifier and re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
            provider = "YouTube"
            source = f"https://www.youtube-nocookie.com/embed/{identifier}?autoplay=0&playsinline=1&rel=0"
    elif _family(host, "tiktok.com"):
        match = re.fullmatch(r"/@[A-Za-z0-9_.-]+/video/(\d{10,24})", path)
        if match:
            provider, vertical = "TikTok", True
            source = f"https://www.tiktok.com/player/v1/{match[1]}?autoplay=0&loop=0"
    elif _family(host, "vimeo.com"):
        match = re.fullmatch(r"/(?:video/)?(\d{1,15})(?:/([a-fA-F0-9]{6,32}))?", path)
        if match:
            provider = "Vimeo"
            values = {"autoplay": "0", "dnt": "1"}
            private_hash = match[2] or query.get("h", [""])[0]
            if re.fullmatch(r"[a-fA-F0-9]{6,32}", private_hash):
                values["h"] = private_hash
            source = f"https://player.vimeo.com/video/{match[1]}?{urllib.parse.urlencode(values)}"
    elif _family(host, "dailymotion.com") or host in {"dai.ly", "www.dai.ly"}:
        match = re.fullmatch(r"/(?:video/|embed/video/)?([A-Za-z0-9]{5,16})(?:_[A-Za-z0-9_-]+)?", path)
        if match:
            provider = "Dailymotion"
            source = f"https://www.dailymotion.com/embed/video/{match[1]}?autoplay=0"
    elif _family(host, "twitch.tv"):
        parent = _parent(parent_host)
        clip = None
        if host == "clips.twitch.tv":
            match = re.fullmatch(r"/([A-Za-z0-9_-]{4,128})", path)
            if match:
                clip = match[1]
        else:
            match = re.fullmatch(r"/[A-Za-z0-9_]{1,25}/clip/([A-Za-z0-9_-]{4,128})", path)
            if match:
                clip = match[1]
        if clip:
            provider = "Twitch"
            source = "https://clips.twitch.tv/embed?" + urllib.parse.urlencode({"clip": clip, "parent": parent, "autoplay": "false"})
        else:
            match = re.fullmatch(r"/videos/(\d{1,24})", path)
            if match:
                provider = "Twitch"
                source = "https://player.twitch.tv/?" + urllib.parse.urlencode({"video": "v" + match[1], "parent": parent, "autoplay": "false"})
    elif _family(host, "facebook.com"):
        match = re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/videos|reel)/(\d{1,24})", path)
        video = match[1] if match else query.get("v", [""])[0] if path == "/watch" else ""
        if re.fullmatch(r"\d{1,24}", video):
            provider = "Facebook"
            canonical = f"https://www.facebook.com/watch/?v={video}"
            source = "https://www.facebook.com/plugins/video.php?" + urllib.parse.urlencode({"href": canonical, "show_text": "false", "autoplay": "false"})
            vertical = bool(match and path.startswith("/reel/"))
    elif _family(host, "instagram.com"):
        match = re.fullmatch(r"/(p|reel|tv)/([A-Za-z0-9_-]{5,32})", path)
        if match:
            provider, vertical = "Instagram", match[1] == "reel"
            source = f"https://www.instagram.com/{match[1]}/{match[2]}/embed/"
    elif _family(host, "bilibili.com"):
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av\d{1,20})", path)
        if match:
            provider = "Bilibili"
            key, identifier = ("bvid", match[1]) if match[1].startswith("BV") else ("aid", match[1][2:])
            source = "https://player.bilibili.com/player.html?" + urllib.parse.urlencode({key: identifier, "autoplay": "0"})

    if source:
        return {"kind": "embed", "media_type": "video", "url": source,
                "provider": provider, "title": f"Vídeo de {provider}", "vertical": vertical,
                "message": "Toque em reproduzir para assistir com som. A plataforma controla a disponibilidade da prévia."}
    return None


def _candidate_formats(info):
    result = []
    for item in info.get("formats") or [info]:
        if item.get("protocol") not in {"https", "http"} or item.get("has_drm"):
            continue
        if not isinstance(item.get("url"), str) or not item["url"].startswith("https://"):
            continue
        extension = str(item.get("ext") or "").lower()
        video, audio = item.get("vcodec"), item.get("acodec")
        if video not in (None, "none") and audio not in (None, "none"):
            if extension in {"mp4", "m4v", "mov"} and not (str(video).startswith(("avc", "h264")) and str(audio).startswith(("mp4a", "aac"))):
                continue
            if extension == "webm" and not (str(video).startswith(("vp8", "vp9", "vp0")) and str(audio).startswith(("opus", "vorbis"))):
                continue
            if "." + extension not in VIDEO_EXTENSIONS:
                continue
            media_type = "video"
        elif video == "none" and audio not in (None, "none") and "." + extension in AUDIO_EXTENSIONS:
            media_type = "audio"
        else:
            continue
        size = item.get("filesize") or item.get("filesize_approx")
        if isinstance(size, (int, float)) and size > engine.MAX_SOURCE:
            continue
        height = item.get("height") or 0
        if isinstance(height, (int, float)) and height > 1080:
            continue
        # Prefer a muxed video over an audio-only alternative; 720p balances
        # preview quality and bandwidth, without downloading a video to inspect.
        rank = (media_type == "video", min(height, 720), -abs(height - 720))
        result.append((rank, item, media_type))
    return [(item, kind) for _, item, kind in sorted(result, key=lambda item: item[0], reverse=True)]


def _readable_media(url, guard, media_type=None):
    public_url(url)
    if urllib.parse.urlsplit(url).scheme != "https":
        raise AudioError("A prévia dentro do site precisa de uma origem HTTPS.", "preview_https", 422)
    with engine.direct_handler(guard) as handler:
        try:
            response = handler.send(MediaRequest(url, method="HEAD"))
        except Exception as exc:
            if "405" not in str(exc) and "501" not in str(exc):
                raise
            response = handler.send(MediaRequest(url, headers={"Range": "bytes=0-0"}))
        with response:
            # Re-validate the final redirect, including its DNS answers. The
            # transport already validates and pins every individual connection.
            public_url(response.url)
            if urllib.parse.urlsplit(response.url).scheme != "https":
                raise AudioError("A origem redirecionou a prévia para HTTP.", "preview_https", 422)
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            extension = Path(urllib.parse.urlsplit(response.url).path).suffix.lower()
            inferred = "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else None
            if inferred is None and mime in {"application/octet-stream", "binary/octet-stream"}:
                inferred = "video" if extension in VIDEO_EXTENSIONS else "audio" if extension in AUDIO_EXTENSIONS else None
            if not inferred or inferred != (media_type or inferred):
                raise AudioError("A origem não retornou uma mídia progressiva compatível.", "preview_unsupported", 422)
            if mime in {"video/mp2t", "application/vnd.apple.mpegurl", "application/x-mpegurl"}:
                raise AudioError("Essa transmissão exige um player próprio da plataforma.", "preview_adaptive", 422)
            if media_type is None and extension not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
                raise AudioError("Esse formato exige um player externo.", "preview_unsupported", 422)
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > engine.MAX_SOURCE:
                raise AudioError("A prévia de arquivo direto aceita até 128 MB.", "source_too_large", 413)
            return response.url, inferred


def resolve_player(url: str, parent_host: str, guard: Guard, cookies: str | None = None):
    public_url(url, resolve=False)
    embedded = official_embed(url, parent_host)
    if embedded:
        # No imported session is attached to an iframe. The provider may use
        # its own browser session according to the user's browser settings.
        if cookies:
            embedded["message"] += " Os cookies fornecidos ao serviço não são usados no player incorporado."
        return embedded

    cookiejar = None
    provider = "Origem"
    try:
        provider = engine.source_for(url)
        cookiejar = parse_netscape(cookies, url, direct_media=provider == "Arquivo direto")
        public_url(url)
        guard.check()
        if provider == "Arquivo direto":
            direct, media_type = _readable_media(url, guard)
            return {"kind": "direct", "media_type": media_type, "url": direct,
                    "provider": provider, "title": engine.direct_title(url),
                    "message": "Toque em reproduzir. O arquivo é transmitido diretamente pela origem, sem conversão."}
        opts = engine.options(guard)
        opts["format"] = "best[height<=720]/best/bestaudio"
        with engine.SafeYoutubeDL(opts, guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
        if not info or info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
            return {"kind": "unavailable", "provider": provider, "message": UNAVAILABLE}
        for candidate, media_type in _candidate_formats(info)[:3]:
            try:
                # Deliberately omit all extracted headers and cookies. A URL
                # needing private headers cannot be played by a browser element.
                direct, media_type = _readable_media(candidate["url"], guard, media_type)
            except AudioError as exc:
                if exc.code in {"unsafe_url", "invalid_url", "dns_failed", "cancelled", "timeout"}:
                    raise
                continue
            except Exception:
                guard.check()
                continue
            return {"kind": "direct", "media_type": media_type, "url": direct,
                    "provider": provider, "title": str(info.get("title") or "Prévia da origem")[:240],
                    "message": "Toque em reproduzir para assistir à prévia. A disponibilidade é controlada pela origem."}
        return {"kind": "unavailable", "provider": provider, "message": UNAVAILABLE}
    except Exception as exc:
        error = engine.translate_error(exc, guard)
        if error.code in {"unsafe_url", "invalid_url", "dns_failed", "cancelled", "timeout"}:
            raise error from exc
        return {"kind": "unavailable", "provider": provider, "message": error.message,
                "code": error.code}
    finally:
        if cookiejar is not None:
            cookiejar.clear()


def register(app):
    # Imported only after index has declared its shared capacity and runner.
    from .index import SLOTS, acquire_slot, run_guarded

    @app.post("/api/player")
    async def source_player(body: PlayerInput, request: Request):
        # Official embeds require no extraction and do not consume a worker.
        embedded = official_embed(body.url, request.url.hostname)
        if embedded:
            if body.cookies:
                embedded["message"] += " Os cookies fornecidos ao serviço não são usados no player incorporado."
            return embedded
        acquire_slot()
        guard = Guard(seconds=25, maximum_bytes=8 * 1024 * 1024)
        try:
            return await run_guarded(request, guard, functools.partial(resolve_player, cookies=body.cookies),
                                     body.url, request.url.hostname)
        finally:
            SLOTS.release()
