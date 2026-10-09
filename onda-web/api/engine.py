"""Stateless media extraction and local-only audio conversion."""
from __future__ import annotations

import functools
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.parse

import imageio_ffmpeg
import yt_dlp
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.downloader.dash import DashSegmentsFD
from yt_dlp.cookies import YoutubeDLCookieJar
from .cookies import RequestCookieJar, parse_netscape
from .direct_transfer import download_direct
from .recovery import AttemptGuard, MAX_ATTEMPTS, retry_kind, wait_for_retry

from .security import AudioError, Guard, PublicRH, public_url

LOGGER = logging.getLogger("onda")
MAX_OUTPUT = 100 * 1024 * 1024
MAX_SOURCE = 128 * 1024 * 1024
VIDEO_RESOLUTIONS = ("source", "360", "480", "720", "1080", "1440", "2160")
MAX_VIDEO_SHORT_EDGE = 2160
MAX_VIDEO_LONG_EDGE = 3840
MAX_VIDEO_PIXELS = MAX_VIDEO_SHORT_EDGE * MAX_VIDEO_LONG_EDGE
# No media-length cap: size, execution time and decoded dimensions remain
# bounded by the actual resources available to this stateless function.
MAX_DURATION = LOSSLESS_DURATION = VIDEO_DURATION = None
LOSSLESS = {"wav", "aiff", "flac"}
FORMATS = {
    "mp3": ("libmp3lame", "audio/mpeg"),
    "m4a": ("aac", "audio/mp4"),
    "wav": ("pcm_s16le", "audio/wav"),
    "flac": ("flac", "audio/flac"),
    "ogg": ("libvorbis", "audio/ogg"),
    "opus": ("libopus", "audio/ogg"),
    "aac": ("aac", "audio/aac"),
    "aiff": ("pcm_s16be", "audio/aiff"),
}
VIDEO_FORMATS = {
    "mp4": ("libx264", "aac", "video/mp4"),
    "webm": ("libvpx-vp9", "libopus", "video/webm"),
    "mkv": ("libx264", "aac", "video/x-matroska"),
    "mov": ("libx264", "aac", "video/quicktime"),
}
# These containers can carry the original compressed stream. Preserving a
# compatible UHD stream is much faster than decoding and encoding every frame.
VIDEO_COPY_CODECS = {
    "mp4": {"h264", "hevc", "av1", "vp9"},
    "mov": {"h264", "hevc"},
    "mkv": {"h264", "hevc", "av1", "vp9", "vp8"},
    "webm": {"vp9", "vp8", "av1"},
}
NATIVE_PROTOCOLS = {"http", "https", "m3u8_native", "http_dash_segments"}
INPUT_FORMATS = "aac,aiff,asf,avi,flac,flv,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mp3,mpeg,mpegts,ogg,wav"


@dataclass(frozen=True)
class SessionOptions:
    """Access hints owned by one request, never by saved media preferences."""
    video_password: str | None = field(default=None, repr=False)
    user_agent: str | None = field(default=None, repr=False)

    def __post_init__(self):
        password = self.video_password
        if password is not None and (not isinstance(password, str) or len(password) > 256
                                     or any(ord(char) < 32 or 127 <= ord(char) <= 159
                                            or 0xD800 <= ord(char) <= 0xDFFF for char in password)):
            raise AudioError("A senha do vídeo deve ter até 256 caracteres e não conter caracteres de controle.",
                             "invalid_video_password", 422)
        agent = self.user_agent
        if agent is not None and (not isinstance(agent, str) or len(agent) > 512
                                  or any(not 32 <= ord(char) <= 126 for char in agent)):
            raise AudioError("O identificador do navegador deve ter até 512 caracteres, sem quebras de linha.",
                             "invalid_user_agent", 422)
        object.__setattr__(self, "video_password", password or None)
        object.__setattr__(self, "user_agent", (agent.strip() or None) if agent is not None else None)

    def extraction_options(self, opts: dict) -> dict:
        result = dict(opts)
        if self.video_password:
            result["videopassword"] = self.video_password
        if self.user_agent:
            result["http_headers"] = {**result.get("http_headers", {}), "User-Agent": self.user_agent}
        return result

    def for_source(self, source: str, url: str):
        if source == "Arquivo direto" and self.video_password:
            raise AudioError("A senha do vídeo só é usada em páginas de plataformas que oferecem esse recurso.",
                             "password_not_supported", 422)
        if self.video_password and urllib.parse.urlsplit(url).scheme != "https":
            raise AudioError("Use um link HTTPS para enviar a senha do vídeo à plataforma.",
                             "password_https_required", 422)
        return self


@dataclass(frozen=True)
class MediaSettings:
    media_type: str = "audio"
    video_resolution: str = "source"
    trim_start: float | None = None
    trim_end: float | None = None
    strip_metadata: bool = True
    mute: bool = False
    normalize_audio: bool = False


@dataclass(frozen=True)
class VideoSources:
    video: Path
    audio: Path | None = None
SOURCE_LABELS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "youtube-nocookie.com": "YouTube",
    "tiktok.com": "TikTok", "soundcloud.com": "SoundCloud", "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion", "dai.ly": "Dailymotion", "twitch.tv": "Twitch",
    "bandcamp.com": "Bandcamp", "mixcloud.com": "Mixcloud", "facebook.com": "Facebook",
    "fb.watch": "Facebook", "instagram.com": "Instagram", "twitter.com": "X",
    "x.com": "X", "reddit.com": "Reddit", "redd.it": "Reddit", "bilibili.com": "Bilibili",
    "archive.org": "Internet Archive",
}
DIRECT_EXTENSIONS = {
    ".mp3", ".m4a", ".mp4", ".webm", ".mov", ".wav", ".flac", ".aac", ".ogg",
    ".opus", ".aif", ".aiff", ".avi", ".mkv", ".mpeg", ".mpg", ".m4v", ".wma", ".ts", ".m2ts",
}


def source_for(url: str) -> str:
    host, _ = public_url(url, resolve=False)
    for extractor in gen_extractor_classes():
        # Generic matches every webpage; the explicit catalog defines supported platforms.
        if extractor.__name__ not in ("GenericIE", "UnsupportedURLIE") and extractor.suitable(url):
            for domain, source in SOURCE_LABELS.items():
                if host == domain or host.endswith("." + domain):
                    return source
            return str(extractor.IE_NAME).split(":", 1)[0]
    if Path(urllib.parse.urlsplit(url).path.lower()).suffix in DIRECT_EXTENSIONS:
        return "Arquivo direto"
    raise AudioError("Esse link não corresponde ao catálogo de plataformas do yt-dlp. Use um link de mídia suportado ou um arquivo direto de áudio/vídeo.", "unsupported_source", 422)


class QuietLogger:
    def debug(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


class ScopedPublicRH(PublicRH):
    def _prepare_headers(self, request, headers):
        super()._prepare_headers(request, headers)
        # Extractors may supply a literal Cookie header. Imported sessions must
        # pass only through the cookie processor's domain/path/HTTPS policy.
        headers.pop("Cookie", None)


class SafeYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, opts, guard, cookiejar=None):
        self.guard = guard
        if cookiejar is not None:
            self.cookiejar = cookiejar
        super().__init__(opts)

    def close(self):
        try:
            super().close()
        finally:
            self.cookiejar.clear()

    def build_request_director(self, handlers, preferences=None):
        # Exactly one handler; extraction cannot fall back to unguarded requests/curl.
        handler = ScopedPublicRH if isinstance(self.cookiejar, RequestCookieJar) else PublicRH
        return super().build_request_director([functools.partial(handler, guard=self.guard)], preferences=[])

    def dl(self, name, info, subtitle=False, test=False):
        try:
            result = self._native_dl(name, info, subtitle=subtitle, test=test)
            if not result[0]:
                raise AudioError("A origem interrompeu a transferência da mídia.", "download_failed", 422)
            return result
        except Exception as exc:
            error = translate_error(exc, self.guard)
            if not getattr(error, "recovery_phase", None):
                error.recovery_phase = "transfer"
            if not getattr(error, "recovery_format_id", None):
                error.recovery_format_id = str(info.get("format_id") or "")[:128]
            # Preserve the upstream cause for distinguishing an expired stream
            # URL from an actual sign-in requirement. Never expose it to clients.
            if error is exc:
                raise
            raise error from exc

    def _native_dl(self, name, info, subtitle=False, test=False):
        downloader = get_suitable_downloader(info, self.params)
        if downloader not in (HttpFD, HlsFD, DashSegmentsFD):
            raise AudioError("Essa mídia exige um método de transferência não suportado.", "unsupported_transfer", 422)
        if downloader is HttpFD:
            # HttpFD's internal range retries cannot verify that a resumed
            # representation is unchanged, and accept approximate 416 sizes.
            # A failed native HTTP track must instead restart in the next
            # isolated attempt. Direct links use our validated If-Range path.
            params = self.params
            self.params = {**params, "continuedl": False, "http_chunk_size": 0, "retries": 0}
            try:
                info = {**info, "downloader_options": {**(info.get("downloader_options") or {}), "http_chunk_size": 0}}
                return super().dl(name, info, subtitle=subtitle, test=test)
            finally:
                self.params = params
        if downloader is HlsFD:
            manifest = info.get("hls_media_playlist_data")
            if manifest is None:
                with self.urlopen(Request(info["url"], headers=info.get("http_headers", {}))) as response:
                    manifest = response.read().decode("utf-8", "replace")
                    info["url"] = response.url
            if not HlsFD.can_download(manifest, info, False):
                raise AudioError("Essa faixa de áudio exige uma transferência não suportada.", "unsupported_transfer", 422)
            info["hls_media_playlist_data"] = manifest
        return super().dl(name, info, subtitle=subtitle, test=test)


def runtime_options():
    os.environ.setdefault("DENO_DIR", "/tmp/onda-deno")
    bundled = Path(__file__).resolve().parents[1] / "bin" / "deno"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return {"deno": {"path": str(bundled)}}
    configured = os.environ.get("YTDLP_JS_RUNTIME", "")
    if configured:
        kind, _, path = configured.partition(":")
        if kind in ("deno", "node"):
            return {kind: {"path": path}} if path else {kind: {}}
    try:
        import deno
        os.environ.setdefault("DENO_DIR", "/tmp/onda-deno")
        return {"deno": {"path": str(deno.find_deno_bin())}}
    except (ImportError, OSError, AttributeError):
        node = shutil.which("node")
        return {"node": {"path": node}} if node else {}


def options(guard: Guard, directory: Path | None = None):
    def progress(info):
        guard.check()
        if info.get("downloaded_bytes", 0) > guard.maximum_bytes:
            raise AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)

    opts = {
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "noplaylist": True, "extract_flat": False, "skip_download": False,
        "cachedir": False, "proxy": "", "socket_timeout": 8,
        # FragmentFD instantiates its own HttpFD, so these safe defaults must
        # also cover HLS/DASH fragments. Fragment-level retries start afresh.
        "retries": 0, "fragment_retries": 2, "extractor_retries": 1,
        "continuedl": False, "skip_unavailable_fragments": False,
        "concurrent_fragment_downloads": 4, "http_chunk_size": 0,
        "max_filesize": guard.maximum_bytes,
        "progress_hooks": [progress], "hls_prefer_native": True,
        "fixup": "never", "writethumbnail": False, "writeinfojson": False,
        # Native HLS must fail closed instead of delegating an URL to FFmpeg.
        "ffmpeg_location": str(Path(__file__).parent / "no-external-ffmpeg"),
        "format": "bestaudio/best[height<=480]/best",
        "outtmpl": str(directory / "source.%(ext)s") if directory else "unused.%(ext)s",
        "js_runtimes": runtime_options(), "compat_opts": {"no-certifi"},
    }
    if getattr(guard, "profile", "default") == "youtube_hls":
        opts["extractor_args"] = {"youtube": {"player_client": ["default", "web_safari"]}}
    return opts


def translate_error(exc: Exception, guard: Guard) -> AudioError:
    if guard.error:
        return guard.error
    if isinstance(exc, AudioError):
        return exc
    text = str(exc).lower()
    if any(token in text for token in ("wrong password", "incorrect password", "invalid password", "password verification failed")):
        return AudioError("A plataforma não aceitou a senha desse vídeo. Confira a senha fornecida pelo criador.",
                          "video_password_invalid", 422)
    if any(token in text for token in ("--video-password", "protected by a password", "password-protected", "password protected")):
        return AudioError("Esse vídeo exige uma senha. Informe a senha do vídeo fornecida pelo criador em Acesso à fonte.",
                          "video_password_required", 422)
    if "429" in text or "too many requests" in text:
        return AudioError("A origem limitou temporariamente as solicitações. Aguarde um pouco e tente novamente.", "upstream_rate_limited", 429)
    if any(token in text for token in ("http error 500", "http error 502", "http error 503", "http error 504", "connection reset", "remote end closed", "incomplete read")):
        return AudioError("A conexão com a origem foi interrompida. Tente novamente em instantes.", "upstream_unavailable", 503)
    if any(token in text for token in ("sign in", "login", "log in", "private", "members-only", "cookies", "not a bot", "403", "401")):
        return AudioError("A plataforma bloqueou o acesso ou exige login. Tente um link público de outra origem.", "platform_blocked", 422)
    if any(token in text for token in ("unavailable", "not available", "removed", "404", "does not exist", "copyright")):
        return AudioError("Esse conteúdo não está disponível ou foi removido na origem.", "unavailable", 422)
    if "format" in text or "no video" in text:
        return AudioError("Não foi possível encontrar uma faixa de áudio disponível nesse link.", "no_audio", 422)
    if "unsupported" in text:
        return AudioError("Esse link não é suportado ou não contém uma mídia pública.", "unsupported_source", 422)
    if "timed out" in text or "timeout" in text:
        return AudioError("A origem demorou demais para responder. Tente novamente.", "upstream_timeout", 504)
    return AudioError("Não foi possível acessar a mídia agora. Confira o link ou tente outra origem.", "upstream_error", 422)


def metadata(info: dict, url: str, source: str) -> dict:
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        raise AudioError("Cole o link de um vídeo específico, sem playlist.", "playlist", 422)
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise AudioError("Transmissões ao vivo não são suportadas. Use um vídeo já publicado.", "live_video", 422)
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        duration = None
    thumbnail = info.get("thumbnail")
    if thumbnail:
        try:
            public_url(thumbnail, resolve=False)
        except AudioError:
            thumbnail = None
    return {
        "title": str(info.get("title") or "Áudio")[:240],
        "duration": duration, "thumbnail": thumbnail,
        "source": source, "webpage_url": url,
    }


def direct_title(url: str):
    return urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).stem)[:240] or "Áudio"


def direct_handler(guard: Guard, *, user_agent: str | None = None):
    headers = {"User-Agent": user_agent} if user_agent else {}
    return PublicRH(guard=guard, logger=QuietLogger(), timeout=8, prefer_system_certs=True, headers=headers)


def verify_media_response(response, url: str):
    kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    extension = Path(urllib.parse.urlsplit(url).path.lower()).suffix
    if (not kind.startswith(("audio/", "video/"))
            and kind not in ("application/octet-stream", "binary/octet-stream")
            and kind not in ("application/ogg", "application/x-ogg")):
        raise AudioError("O link direto precisa retornar um arquivo de áudio ou vídeo.", "not_media", 422)
    if extension not in DIRECT_EXTENSIONS:
        raise AudioError("O endereço redirecionou para um formato de arquivo não suportado.", "not_media", 422)


def inspect_media(url: str, guard: Guard, cookies: str | None = None, media_type: str = "audio",
                  session: SessionOptions | None = None):
    cookiejar = None
    try:
        source = source_for(url)
        session = (session or SessionOptions()).for_source(source, url)
        cookiejar = parse_netscape(cookies, url, direct_media=source == "Arquivo direto")
        public_url(url)
        guard.check()
        if source == "Arquivo direto":
            factory = functools.partial(direct_handler, user_agent=session.user_agent) if session.user_agent else direct_handler
            with factory(guard) as handler:
                try:
                    response = handler.send(Request(url, method="HEAD"))
                except HTTPError as exc:
                    exc.response.close()
                    # Some origins implement GET but not HEAD. Match the
                    # actual status, never an incidental number in a URL or
                    # error message, and release the rejected response first.
                    if exc.status not in (405, 501):
                        raise
                    response = handler.send(Request(url, headers={"Range": "bytes=0-0"}))
                with response:
                    verify_media_response(response, response.url)
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > MAX_SOURCE:
                        raise AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)
            return {"title": direct_title(url), "duration": None, "thumbnail": None,
                    "source": source, "webpage_url": url}
        with SafeYoutubeDL(session.extraction_options(options(guard)), guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
        if not info:
            raise AudioError("Não foi possível identificar esse áudio.")
        return metadata(info, url, source)
    except Exception as exc:
        error = translate_error(exc, guard)
        if error is exc:
            raise
        raise error from exc
    finally:
        if cookiejar is not None:
            cookiejar.clear()


def direct_download(url: str, directory: Path, guard: Guard, *, session: SessionOptions | None = None):
    factory = functools.partial(direct_handler, user_agent=session.user_agent) if session and session.user_agent else direct_handler
    return download_direct(url, directory, guard, handler_factory=factory,
                           verify_response=verify_media_response, title_factory=direct_title)


def acquire_media(url: str, directory: Path, guard: Guard, audio_format: str, cookies: str | None = None,
                  session: SessionOptions | None = None):
    source_name = source_for(url)
    session = (session or SessionOptions()).for_source(source_name, url)
    cookiejar = parse_netscape(cookies, url, direct_media=source_name == "Arquivo direto")
    try:
        if source_name == "Arquivo direto":
            # source_for already validates the authority. Resolve and pin
            # direct media inside its bounded transfer attempts, so an initial
            # temporary DNS failure can recover before any TCP connection.
            return direct_download(url, directory, guard, **({"session": session} if session.user_agent else {}))
        public_url(url)
        with SafeYoutubeDL(session.extraction_options(options(guard, directory)), guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
            if not info:
                raise AudioError("Não foi possível identificar esse áudio.")
            details = metadata(info, url, source_name)
            # Force only native downloaders. FFmpeg must never receive an internet URL.
            formats = info.get("formats") or [info]
            excluded = getattr(guard, "excluded_formats", frozenset())
            info["formats"] = [item for item in formats if item.get("protocol") in
                               ("http", "https", "m3u8_native", "http_dash_segments") and not item.get("has_drm")
                               and str(item.get("format_id") or "") not in excluded]
            if not info["formats"]:
                raise AudioError("Não há uma faixa de áudio pública compatível nesse vídeo.", "no_audio", 422)
            downloader.process_ie_result(info, download=True)
        files = [path for path in directory.glob("source.*") if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
        if len(files) != 1 or not files[0].stat().st_size:
            raise AudioError("A plataforma não entregou o áudio completo. Tente outra origem.", "download_failed", 422)
        if files[0].stat().st_size > guard.maximum_bytes:
            raise AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)
        return files[0], details
    finally:
        if cookiejar is not None:
            cookiejar.clear()


def estimated_size(item: dict, duration: float | None) -> float | None:
    size = item.get("filesize") or item.get("filesize_approx")
    if size and size > 0:
        return float(size)
    bitrate = item.get("tbr") or item.get("vbr") or item.get("abr")
    return float(bitrate) * 1000 * duration / 8 if bitrate and duration else None


def video_resolution_edge(item: dict) -> int:
    """Use the short edge so landscape and portrait resolutions agree."""
    width, height = item.get("width"), item.get("height")
    return min(width, height) if width and height else height or width or 0


def video_dimensions_supported(width: int | None, height: int | None) -> bool:
    """Bound known dimensions; verify incomplete extractor metadata after download."""
    if not width or not height:
        return (width or height or 0) <= MAX_VIDEO_LONG_EDGE
    return (min(width, height) <= MAX_VIDEO_SHORT_EDGE
            and max(width, height) <= MAX_VIDEO_LONG_EDGE
            and width * height <= MAX_VIDEO_PIXELS)


def canonical_video_codec(codec: str | None) -> str:
    """Normalize extractor codec strings before choosing a native stream."""
    codec = (codec or "").lower()
    if codec.startswith(("avc1", "avc3", "h264")):
        return "h264"
    if codec.startswith(("hev1", "hvc1", "hevc", "h265")):
        return "hevc"
    if codec.startswith(("av01", "av1")):
        return "av1"
    if codec.startswith(("vp09", "vp9")):
        return "vp9"
    if codec.startswith(("vp08", "vp8")):
        return "vp8"
    return codec


def native_video_score(item: dict, video_format: str, cap: int) -> int:
    codec = canonical_video_codec(item.get("vcodec"))
    if codec not in VIDEO_COPY_CODECS[video_format] or (item.get("fps") or 0) > 30:
        return 0
    width, height = item.get("width"), item.get("height")
    if width and height and (min(width, height) > cap or max(width, height) > (int(cap * 16 / 9) // 2) * 2):
        return 0
    # At the same resolution, prefer widely playable native H.264 for MP4/MOV.
    # Higher-resolution VP9/AV1/HEVC still wins over a lower-resolution source.
    return 2 if codec == "h264" and video_format in ("mp4", "mov") else 1


def select_video_formats(info: dict, resolution: str, available_bytes: int, mute=False, video_format="mp4"):
    """Select native streams ourselves; yt-dlp never gets a merge request."""
    safe = [item for item in info.get("formats", [info]) if item.get("protocol") in NATIVE_PROTOCOLS
            and not item.get("has_drm") and item.get("url")]
    audio = [item for item in safe if item.get("vcodec") == "none" and item.get("acodec") not in (None, "none")]
    audio_codec = "opus" if video_format == "webm" else "aac"
    best_audio = max(audio, key=lambda item: (item.get("abr") or item.get("tbr") or 0,
                                            (item.get("acodec") or "").startswith(audio_codec),
                                            item.get("asr") or 0), default=None)
    videos = [item for item in safe if item.get("vcodec") not in (None, "none")]
    if not videos:
        raise AudioError("Esse link não contém uma faixa de vídeo disponível.", "no_video", 422)
    cap = MAX_VIDEO_SHORT_EDGE if resolution == "source" else int(resolution)
    bounded_videos = [item for item in videos if video_dimensions_supported(item.get("width"), item.get("height"))]
    if not bounded_videos:
        raise AudioError("A origem só oferece vídeo acima de 2160p (4K UHD). Use uma origem com resolução de até 4K.", "unavailable_resolution", 422)
    # Size estimates reserve a little room for metadata/manifests. When needed,
    # choose a smaller source before spending the shared transfer budget.
    caps = list(dict.fromkeys([cap] + [level for level in (1440, 1080, 720, 480, 360) if level < cap]))
    for height in caps:
        candidates = [item for item in bounded_videos if video_resolution_edge(item) <= height]
        candidates.sort(key=lambda item: (video_resolution_edge(item), (item.get("width") or 0) * (item.get("height") or 0),
                                          native_video_score(item, video_format, cap),
                                          item.get("acodec") not in (None, "none"),
                                          item.get("tbr") or item.get("vbr") or 0), reverse=True)
        for video in candidates:
            # Muxed formats already include sound. Video-only formats receive
            # a separate native audio download, unless the user requested mute.
            separate_audio = None if mute or video.get("acodec") not in (None, "none") else best_audio
            sizes = [estimated_size(video, info.get("duration"))]
            if separate_audio:
                sizes.append(estimated_size(separate_audio, info.get("duration")))
            if all(value is not None for value in sizes) and sum(sizes) > available_bytes * .96:
                continue
            return video, separate_audio
    # The requested resolution describes the output. A single source up to
    # 4K UHD can still be downloaded safely and reduced to a smaller size locally.
    for video in sorted(bounded_videos, key=lambda item: (video_resolution_edge(item), item.get("tbr") or 0)):
        separate_audio = None if mute or video.get("acodec") not in (None, "none") else best_audio
        sizes = [estimated_size(video, info.get("duration"))]
        if separate_audio:
            sizes.append(estimated_size(separate_audio, info.get("duration")))
        if all(value is not None for value in sizes) and sum(sizes) > available_bytes * .96:
            continue
        return video, separate_audio
    raise AudioError("A origem do vídeo ultrapassa 128 MB, mesmo em uma resolução menor. Use um vídeo menor.", "source_too_large", 413)


def clone_download_cookies(cookiejar):
    """Each parallel downloader owns its jar, including imported scope policy."""
    cloned = (RequestCookieJar(cookiejar._policy.scopes) if isinstance(cookiejar, RequestCookieJar)
              else YoutubeDLCookieJar())
    for cookie in cookiejar:
        cloned.set_cookie(copy.copy(cookie))
    return cloned


def acquire_video_media(url: str, directory: Path, guard: Guard, video_resolution="source", cookies=None, mute=False,
                        video_format="mp4", session: SessionOptions | None = None):
    source_name = source_for(url)
    session = (session or SessionOptions()).for_source(source_name, url)
    cookiejar = parse_netscape(cookies, url, direct_media=source_name == "Arquivo direto")
    try:
        if source_name == "Arquivo direto":
            path, details = direct_download(url, directory, guard, **({"session": session} if session.user_agent else {}))
            return VideoSources(path), details
        public_url(url)
        opts = session.extraction_options(options(guard, directory))
        # Extraction lists every format. Downloads below receive individual
        # streams, not the bestvideo+bestaudio merger path.
        opts["format"] = "bestvideo/best/bestaudio"
        with SafeYoutubeDL(opts, guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
            if not info:
                raise AudioError("Não foi possível identificar esse vídeo.", "no_video", 422)
            details = metadata(info, url, source_name)
            excluded = getattr(guard, "excluded_formats", frozenset())
            if excluded:
                info = {**info, "formats": [item for item in info.get("formats", [info])
                                            if str(item.get("format_id") or "") not in excluded]}
            video, audio = select_video_formats(info, str(video_resolution), guard.maximum_bytes - guard.received, mute,
                                                video_format=video_format)
            video_path = directory / "source-video.media"
            audio_path = directory / "source-audio.media" if audio else None
            streams = []
            for item, path in ((video, video_path), (audio, audio_path)):
                if item is None:
                    continue
                guard.check()
                # Exclude selected/merged format bookkeeping inherited from extraction.
                stream_info = {key: value for key, value in info.items() if key not in
                               ("formats", "requested_formats", "requested_downloads", "url", "protocol", "ext", "format_id")}
                stream_info.update(item)
                stream_info["http_headers"] = downloader._calc_headers(stream_info)
                # Resolve both stream destinations before opening either one.
                # Parallel work still shares Guard's aggregate byte/deadline
                # accounting and uses the same pinned public network handler.
                public_url(stream_info["url"])
                streams.append((stream_info, path))
            streams = [(stream_info, path, clone_download_cookies(downloader.cookiejar))
                       for stream_info, path in streams]

            def download_stream(stream):
                stream_info, path, stream_cookies = stream
                guard.check()
                with SafeYoutubeDL(dict(opts), guard, cookiejar=stream_cookies) as stream_downloader:
                    success, _ = stream_downloader.dl(str(path), stream_info)
                if not success or not path.is_file() or not path.stat().st_size:
                    raise AudioError("A origem não entregou o vídeo completo. Tente outra resolução ou origem.", "download_failed", 422)
                return path

            try:
                if len(streams) == 1:
                    download_stream(streams[0])
                else:
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="onda-track") as pool:
                        futures = [pool.submit(download_stream, stream) for stream in streams]
                        try:
                            for future in as_completed(futures):
                                future.result()
                        except Exception as exc:
                            with guard.byte_lock:
                                guard.error = guard.error or translate_error(exc, guard)
                            guard.abort()
                            for future in futures:
                                future.cancel()
                            raise
            finally:
                for _, _, stream_cookies in streams:
                    stream_cookies.clear()
            paths = [video_path] + ([audio_path] if audio_path else [])
            if sum(path.stat().st_size for path in paths) > guard.maximum_bytes:
                raise AudioError("As faixas de origem do vídeo ultrapassam 128 MB.", "source_too_large", 413)
            details["resolution"] = video.get("height")
            return VideoSources(video_path, audio_path), details
    finally:
        if cookiejar is not None:
            cookiejar.clear()


def input_command(source: Path):
    return ["-protocol_whitelist", "file,pipe", "-format_whitelist", INPUT_FORMATS, "-i", str(source)]


@functools.lru_cache(maxsize=1)
def video_worker_threads() -> int:
    """Use available CPU without allocating unbounded UHD frame workers."""
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max" and int(period) > 0:
            available = min(available, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    # Two workers improve cloud encoding without the memory cost of FFmpeg's
    # host-CPU autodetection, which can exceed the function's actual CPU quota.
    return max(1, min(2, available))


def sanitize_transport_metadata(source: Path, guard: Guard):
    """Remove informational DVB SDT/BAT packets from local transport streams.

    The bundled FFmpeg 7.0.2 static build crashes while parsing SDT service
    descriptors. PID 0x11 carries those descriptions, never video/audio, PAT,
    PMT or PCR. Every other packet (including M2TS timestamps/FEC) stays intact.
    This is an atomic, bounded workaround until the bundled runtime is replaced.
    """
    guard.check()
    with source.open("rb") as original:
        header = original.read(4096 + 5 * 204)
        size = source.stat().st_size
        shape = next(((stride, offset, prefix) for stride, offset in ((188, 0), (192, 4), (204, 0))
                      for prefix in range(min(4097, max(0, len(header) - stride * 5 + 1)))
                      if (size - prefix) % stride == 0 and
                      all(header[prefix + offset + stride * index] == 0x47 for index in range(5))), None)
        if not shape:
            return
        stride, offset, prefix = shape
        temporary = source.with_name(source.name + ".transport-safe")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        removed = 0
        try:
            original.seek(prefix)
            with os.fdopen(descriptor, "wb") as cleaned:
                cleaned.write(header[:prefix])
                while block := original.read(stride * 4096):
                    guard.check()
                    for start in range(0, len(block), stride):
                        packet = block[start:start + stride]
                        if packet[offset] != 0x47:
                            raise AudioError("Esse arquivo de transporte está incompleto ou corrompido.", "conversion_failed", 422)
                        pid = ((packet[offset + 1] & 0x1f) << 8) | packet[offset + 2]
                        if pid == 0x11:
                            removed += 1
                        else:
                            cleaned.write(packet)
            guard.check()
            if removed:
                os.replace(temporary, source)
        finally:
            temporary.unlink(missing_ok=True)


def probe_media(source: Path, guard: Guard):
    """Read local container information without adding an ffprobe dependency."""
    guard.check()
    sanitize_transport_metadata(source, guard)
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin"] + input_command(source)
    log = source.parent / f"probe-{source.name}.log"
    process = None
    started = time.monotonic()
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=output)
            while process.poll() is None:
                guard.check()
                if time.monotonic() - started > 8:
                    raise AudioError("Não foi possível analisar a mídia dentro do prazo.", "timeout", 504)
                if log.stat().st_size > 1024 * 1024:
                    raise AudioError("Os metadados dessa mídia ultrapassam o limite seguro.", "conversion_failed", 422)
                time.sleep(.02)
        if log.stat().st_size > 1024 * 1024:
            raise AudioError("Os metadados dessa mídia ultrapassam o limite seguro.", "conversion_failed", 422)
        text = log.read_text(errors="replace")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    guard.check()
    # Metadata values can contain strings that look like FFmpeg diagnostics.
    # Real stream/duration records start with exactly two spaces; metadata
    # values are printed with their own label/continuation indentation.
    duration_match = re.search(r"^  Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text, re.M)
    duration = sum(float(value) * multiplier for value, multiplier in zip(duration_match.groups(), (3600, 60, 1))) if duration_match else None
    streams = list(re.finditer(r"^  Stream #0:(\d+)[^:\r\n]*:\s+(Video|Audio):\s+([^\r\n]+)$", text, re.M))
    video_stream = next((item for item in streams if item[2] == "Video" and "attached pic" not in item[3]), None)
    audio_stream = next((item for item in streams if item[2] == "Audio"), None)
    video = video_stream[3] if video_stream else None
    audio = audio_stream[3] if audio_stream else None
    dimensions = re.search(r"(?:^|,)\s*([1-9]\d{0,5})x([1-9]\d{0,5})(?:[\s,]|$)", video or "")
    fps = re.search(r"(\d+(?:\.\d+)?) fps", video or "")
    aspect = re.search(r"\[SAR (\d+):(\d+)", video or "")
    rotation = re.search(r"^ {4,6}displaymatrix: rotation of (-?\d+(?:\.\d+)?) degrees", text, re.M)
    return {"duration": duration, "video": bool(video), "audio": any(item[2] == "Audio" for item in streams),
            "width": int(dimensions[1]) if dimensions else None,
            "height": int(dimensions[2]) if dimensions else None,
            "fps": float(fps[1]) if fps else None, "video_index": int(video_stream[1]) if video_stream else 0,
            "video_codec": video.split()[0].rstrip(",") if video else None,
            "audio_codec": audio.split()[0].rstrip(",") if audio else None,
            "pixel_format": (match[1] if (match := re.search(r",\s*(yuv420p(?:10le)?)(?:[,(\s]|$)", video or "")) else None),
            "sample_aspect_ratio": (int(aspect[1]), int(aspect[2])) if aspect else None,
            "rotation": float(rotation[1]) if rotation else 0}


def validate_trim(settings: MediaSettings, duration: float | None):
    start = settings.trim_start or 0
    if duration and (start >= duration or settings.trim_end is not None and settings.trim_end > duration + .05):
        raise AudioError("O corte precisa ficar dentro da duração da mídia.", "invalid_trim", 422)
    # Unknown duration is valid for finite files. FFmpeg reads until EOF, with
    # the same output-size and wall-clock budget as a known-duration source.
    return start, settings.trim_end - start if settings.trim_end is not None else None


def metadata_command(strip: bool):
    return (["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"] if strip
            else ["-map_metadata", "0", "-map_chapters", "0"])


def run_conversion(command, target, directory, guard):
    process = None
    log = directory / "ffmpeg.log"
    progress_path = directory / "progress.log"
    command += ["-progress", str(progress_path), str(target)]
    try:
        with log.open("wb") as error_output:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=error_output)
            while process.poll() is None:
                guard.check()
                if target.exists() and target.stat().st_size > MAX_OUTPUT:
                    raise AudioError("A mídia convertida ultrapassa 100 MB. Escolha uma resolução menor ou uma mídia mais curta.", "output_too_large", 413)
                if log.stat().st_size > 1024 * 1024:
                    raise AudioError("Essa mídia contém erros e não pode ser convertida.", "conversion_failed", 422)
                time.sleep(.1)
        if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise AudioError("A mídia não contém as faixas necessárias ou não pode ser convertida.", "conversion_failed", 422)
        # A successful mux can still create only a container header when a
        # requested cut starts after EOF and the source has no known duration.
        # One video frame at PTS zero is valid even when its out_time_us is zero.
        progress = progress_path.read_text(errors="replace") if progress_path.is_file() else ""
        frames = re.findall(r"^frame=(\d+)$", progress, re.M)
        times = re.findall(r"^out_time_us=(-?\d+)$", progress, re.M)
        if not any(int(value) > 0 for value in frames + times):
            raise AudioError("O trecho escolhido não contém áudio ou vídeo. Confira o início e o fim do corte.", "conversion_failed", 422)
        if target.stat().st_size > MAX_OUTPUT:
            raise AudioError("A mídia convertida ultrapassa 100 MB. Escolha uma resolução menor ou uma mídia mais curta.", "output_too_large", 413)
        guard.check()
        return target
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def convert_audio(source: Path, directory: Path, audio_format: str, quality: int | str, guard: Guard, settings: MediaSettings | None = None):
    settings = settings or MediaSettings()
    inspected = probe_media(source, guard)
    if not inspected["audio"]:
        raise AudioError("Esse arquivo não contém uma faixa de áudio válida.", "no_audio", 422)
    start, output_duration = validate_trim(settings, inspected["duration"])
    target = directory / f"audio.{audio_format}"
    codec, _ = FORMATS[audio_format]
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-nostats", "-nostdin", "-y"]
    command += input_command(source)
    # Decode before seeking: native HLS/MPEG-TS downloads can lack an indexed
    # keyframe at the requested position. Input seeking can silently lose video
    # or audio; output seeking also keeps independently downloaded tracks aligned.
    if start:
        command += ["-ss", str(start)]
    command += ["-map", "0:a:0", "-vn", "-sn", "-dn"]
    command += metadata_command(settings.strip_metadata)
    # Preserve original samples for a compatible lossless source. A selected
    # lossy bitrate is always encoded; an average bitrate is not a guarantee
    # that a copied stream meets the user's requested encoding settings.
    copy_audio = (quality == "source" and audio_format in LOSSLESS and not start
                  and output_duration is None and not settings.normalize_audio
                  and inspected.get("audio_codec") == codec)
    if copy_audio:
        command += ["-c:a", "copy"]
    else:
        command += ["-threads", "1", "-ac", "2", "-ar", "48000" if audio_format == "opus" else "44100", "-c:a", codec]
        if settings.normalize_audio:
            command += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        if audio_format not in LOSSLESS:
            command += ["-b:a", f"{quality}k"]
        elif audio_format == "flac":
            command += ["-sample_fmt", "s16"]
    if output_duration is not None:
        command += ["-t", str(output_duration)]
    if audio_format == "m4a":
        command += ["-movflags", "+faststart"]
    if audio_format == "aac":
        command += ["-f", "adts"]
    return run_conversion(command, target, directory, guard)


def convert_video(source: VideoSources | Path, directory: Path, video_format: str, quality: int | str,
                  guard: Guard, settings: MediaSettings):
    sources = source if isinstance(source, VideoSources) else VideoSources(source)
    inspected = probe_media(sources.video, guard)
    if not inspected["video"] or not inspected["width"] or not inspected["height"]:
        raise AudioError("Esse arquivo não contém uma faixa de vídeo válida.", "no_video", 422)
    if min(inspected["width"], inspected["height"]) < 2:
        raise AudioError("As dimensões desse vídeo não são suportadas.", "no_video", 422)
    if not video_dimensions_supported(inspected["width"], inspected["height"]):
        raise AudioError("A fonte ultrapassa a resolução de 2160p (4K UHD) suportada. Use uma origem de até 4K.", "unavailable_resolution", 422)
    start, output_duration = validate_trim(settings, inspected["duration"])
    audio_inspected = inspected
    if sources.audio:
        audio_inspected = probe_media(sources.audio, guard)
        if not audio_inspected["audio"]:
            raise AudioError("A origem não entregou uma faixa de áudio válida.", "no_audio", 422)
        validate_trim(settings, audio_inspected["duration"])
    cap = MAX_VIDEO_SHORT_EDGE if settings.video_resolution == "source" else int(settings.video_resolution)
    # Bounds never increase the source dimensions. H.264 requires even pixels.
    long_edge = (int(cap * 16 / 9) // 2) * 2
    # iw/ih are evaluated after FFmpeg's automatic display-matrix rotation.
    # Phone videos may be coded as landscape pixels but displayed as portrait.
    filters = (f"scale=w='if(gte(iw,ih),min(iw,{long_edge}),min(iw,{cap}))':"
               f"h='if(gte(iw,ih),min(ih,{cap}),min(ih,{long_edge}))':"
               "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1")
    fps = min(inspected["fps"] or 30, 30)
    video_codec, audio_codec, _ = VIDEO_FORMATS[video_format]
    compatible_video_codecs = VIDEO_COPY_CODECS[video_format]
    compatible_audio_codec = "opus" if video_format == "webm" else "aac"
    # Copy only streams whose decoded presentation already meets the output
    # contract. Seeking, scaling, rotation and normalization retain the precise
    # encode path; copying would otherwise silently lose the requested changes.
    copy_video = (not getattr(guard, "force_encode", False) and not start and output_duration is None
                  and inspected.get("video_codec") in compatible_video_codecs
                  and inspected.get("pixel_format") in ("yuv420p", "yuv420p10le")
                  and inspected.get("fps") is not None and inspected["fps"] <= 30
                  and inspected["width"] % 2 == inspected["height"] % 2 == 0
                  and min(inspected["width"], inspected["height"]) <= cap
                  and max(inspected["width"], inspected["height"]) <= long_edge
                  and inspected.get("sample_aspect_ratio") in (None, (1, 1))
                  and not inspected.get("rotation"))
    copy_audio = (not getattr(guard, "force_encode", False) and quality == "source" and not start and output_duration is None
                  and not settings.normalize_audio
                  and audio_inspected.get("audio_codec") == compatible_audio_codec)
    # Reserve a small container overhead before copying, without forcing a
    # full UHD encode for a 95–99 MB source that fits the 100 MB output budget.
    # Count all source bytes conservatively, plus newly encoded audio; the
    # running conversion still enforces the exact hard output-size limit.
    estimated_output = sources.video.stat().st_size
    if sources.audio and not settings.mute:
        estimated_output += sources.audio.stat().st_size
    if not settings.mute and not copy_audio and inspected["duration"]:
        estimated_output += inspected["duration"] * (192 if quality == "source" else int(quality)) * 1000 / 8
    if estimated_output > MAX_OUTPUT * .99:
        copy_video = False
    guard.copy_attempted = copy_video or (copy_audio and not settings.mute)
    target = directory / f"video.{video_format}"
    threads = video_worker_threads()
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-nostats", "-nostdin", "-y"]
    command += ["-filter_threads", str(threads), "-threads", str(threads)]
    command += input_command(sources.video)
    if sources.audio and not settings.mute:
        command += ["-threads", "1"]
        command += input_command(sources.audio)
    # Seek once after every input. Fast input seeking may skip the only TS
    # keyframe and still return success with an audio-only output.
    if start:
        command += ["-ss", str(start)]
    command += ["-map", f"0:{inspected['video_index']}"]
    if settings.mute:
        command += ["-an"]
    else:
        command += ["-map", "1:a:0?" if sources.audio else "0:a:0?"]
        if copy_audio:
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", audio_codec, "-b:a", f"{192 if quality == 'source' else quality}k", "-ac", "2", "-ar", "48000"]
            if settings.normalize_audio:
                command += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    command += ["-sn", "-dn"] + metadata_command(settings.strip_metadata)
    effective_duration = (settings.trim_end - start if settings.trim_end is not None
                          else max(.1, inspected["duration"] - start) if inspected["duration"] else None)
    if copy_video:
        command += ["-c:v", "copy"]
        if inspected.get("video_codec") == "hevc" and video_format in ("mp4", "mov"):
            command += ["-tag:v", "hvc1"]
    else:
        # Increase the encoding budget for QHD/UHD rather than squeezing 4K
        # into the former 1080p bitrate. Actual output pixels determine the
        # ceiling; choosing 4K for a smaller source never increases its size.
        scale = min(1, cap / min(inspected["width"], inspected["height"]),
                    long_edge / max(inspected["width"], inspected["height"]))
        output_pixels = inspected["width"] * inspected["height"] * scale * scale
        bitrate_ceiling = max(3500, round(3500 * output_pixels / (1920 * 1080)))
        video_bitrate = (max(64, min(bitrate_ceiling, int(MAX_OUTPUT * .8 * 8 / effective_duration / 1000)
                                                    - (0 if settings.mute else 320)))
                         if effective_duration else bitrate_ceiling)
        command += ["-vf", filters, "-r", f"{fps:g}", "-c:v", video_codec, "-pix_fmt", "yuv420p",
                    "-b:v", f"{video_bitrate}k", "-threads:v", str(threads), "-threads:a", "1"]
        if video_codec == "libx264":
            command += ["-preset", "ultrafast", "-maxrate", f"{video_bitrate}k", "-bufsize", f"{video_bitrate * 2}k"]
        else:
            command += ["-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
                        "-tile-columns", "1" if threads > 1 else "0"]
    if output_duration is not None:
        command += ["-t", str(output_duration)]
    if video_format in ("mp4", "mov"):
        command += ["-movflags", "+faststart"]
    result = run_conversion(command, target, directory, guard)
    verified = probe_media(result, guard)
    if not verified["video"]:
        raise AudioError("A conversão não produziu uma faixa de vídeo válida.", "conversion_failed", 422)
    expects_audio = not settings.mute and (sources.audio is not None or inspected["audio"])
    if settings.mute and verified["audio"] or expects_audio and not verified["audio"]:
        raise AudioError("A conversão não produziu as faixas de áudio esperadas.", "conversion_failed", 422)
    guard.output_info = verified
    return result


def acquire_with_recovery(url, directory, audio_format, guard, settings, cookies,
                          session: SessionOptions | None = None):
    """Recover native extraction without resetting request limits or sharing files."""
    source_name = source_for(url)
    session_args = {"session": session} if session and (session.video_password or session.user_agent) else {}

    def acquire(folder, attempt_guard):
        if settings.media_type == "video":
            return acquire_video_media(url, folder, attempt_guard, settings.video_resolution,
                                       cookies=cookies, mute=settings.mute, video_format=audio_format, **session_args)
        return acquire_media(url, folder, attempt_guard, audio_format, cookies=cookies, **session_args)

    # The direct transfer owns its three HTTP requests and safe range resume.
    if source_name == "Arquivo direto":
        return acquire(directory, guard)

    excluded, profile, refreshes = set(), "default", 0
    for number in range(1, MAX_ATTEMPTS + 1):
        guard.check()
        folder = directory / f"attempt-{number}"
        folder.mkdir()
        attempt_guard = AttemptGuard(guard, attempt=number, profile=profile,
                                     excluded_formats=frozenset(excluded))
        complete = False
        try:
            sources, details = acquire(folder, attempt_guard)
            details["recovery"] = {"attempts": number, "method": profile, "resumed": False}
            complete = True
            return sources, details
        except Exception as exc:
            kind = retry_kind(exc)
            already_exhausted = bool(getattr(exc, "recovery_exhausted", False))
            error = translate_error(exc, attempt_guard)
            error.recovery_attempts = number
            if (kind is None or number == MAX_ATTEMPTS or already_exhausted
                    or guard.remaining < 8):
                error.recovery_exhausted = bool(kind) or already_exhausted
                if error is exc:
                    raise
                raise error from exc
            format_id = getattr(exc, "recovery_format_id", "")
            if kind == "refresh" and refreshes == 0:
                # Re-extract once before discarding a potentially expired URL.
                refreshes += 1
            elif kind in ("alternate", "refresh"):
                if format_id:
                    excluded.add(format_id)
                profile = "youtube_hls" if source_name == "YouTube" else "native_alternative"
            if not wait_for_retry(guard, number, exc):
                error.recovery_exhausted = True
                if error is exc:
                    raise
                raise error from exc
        finally:
            attempt_guard.close_sockets()
            if not complete:
                shutil.rmtree(folder, ignore_errors=True)


def prepare_download(url, directory, audio_format, quality, guard, cookies: str | None = None,
                     settings: MediaSettings | None = None, session: SessionOptions | None = None):
    settings = settings or MediaSettings()
    try:
        session_args = {"session": session} if session and (session.video_password or session.user_agent) else {}
        sources, details = acquire_with_recovery(url, directory, audio_format, guard, settings, cookies, **session_args)
        if settings.media_type == "video":
            try:
                target = convert_video(sources, directory, audio_format, quality, guard, settings)
                converted = guard
            except AudioError as exc:
                if exc.code != "conversion_failed" or not getattr(guard, "copy_attempted", False) or guard.remaining < 8:
                    raise
                converted = AttemptGuard(guard, profile="compatibility", force_encode=True)
                target = convert_video(sources, directory, audio_format, quality, converted, settings)
                details.setdefault("recovery", {"attempts": 1, "resumed": False})
                details["recovery"]["method"] = "compatibility"
                details["recovery"]["conversion_recovered"] = True
            output = getattr(converted, "output_info", {})
            if output.get("width") and output.get("height"):
                details["output_resolution"] = min(output["width"], output["height"])
            inputs = ([sources.video, sources.audio] if isinstance(sources, VideoSources) else [sources])
            for path in inputs:
                if path:
                    path.unlink(missing_ok=True)
            return target, details
        source = sources
        if settings == MediaSettings():
            target = convert_audio(source, directory, audio_format, quality, guard)
        else:
            target = convert_audio(source, directory, audio_format, quality, guard, settings=settings)
        source.unlink(missing_ok=True)
        return target, details
    except Exception as exc:
        error = translate_error(exc, guard)
        if error is exc:
            raise
        raise error from exc
