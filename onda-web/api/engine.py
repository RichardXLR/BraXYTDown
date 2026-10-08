"""Stateless media extraction and local-only audio conversion."""
from __future__ import annotations

import functools
from dataclasses import dataclass
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
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.downloader.dash import DashSegmentsFD
from .cookies import RequestCookieJar, parse_netscape

from .security import AudioError, Guard, PublicRH, public_url

LOGGER = logging.getLogger("onda")
MAX_OUTPUT = 100 * 1024 * 1024
MAX_SOURCE = 128 * 1024 * 1024
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
NATIVE_PROTOCOLS = {"http", "https", "m3u8_native", "http_dash_segments"}
INPUT_FORMATS = "aac,aiff,asf,avi,flac,flv,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mp3,mpeg,mpegts,ogg,wav"


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
        downloader = get_suitable_downloader(info, self.params)
        if downloader not in (HttpFD, HlsFD, DashSegmentsFD):
            raise AudioError("Essa mídia exige um método de transferência não suportado.", "unsupported_transfer", 422)
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

    return {
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "noplaylist": True, "extract_flat": False, "skip_download": False,
        "cachedir": False, "proxy": "", "socket_timeout": 8,
        "retries": 1, "fragment_retries": 1, "extractor_retries": 1,
        "concurrent_fragment_downloads": 4, "http_chunk_size": 1024 * 1024,
        "max_filesize": guard.maximum_bytes,
        "progress_hooks": [progress], "hls_prefer_native": True,
        "fixup": "never", "writethumbnail": False, "writeinfojson": False,
        # Native HLS must fail closed instead of delegating an URL to FFmpeg.
        "ffmpeg_location": str(Path(__file__).parent / "no-external-ffmpeg"),
        "format": "bestaudio/best[height<=480]/best",
        "outtmpl": str(directory / "source.%(ext)s") if directory else "unused.%(ext)s",
        "js_runtimes": runtime_options(), "compat_opts": {"no-certifi"},
    }


def translate_error(exc: Exception, guard: Guard) -> AudioError:
    if guard.error:
        return guard.error
    if isinstance(exc, AudioError):
        return exc
    text = str(exc).lower()
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


def direct_handler(guard: Guard):
    return PublicRH(guard=guard, logger=QuietLogger(), timeout=8, prefer_system_certs=True)


def verify_media_response(response, url: str):
    kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    extension = Path(urllib.parse.urlsplit(url).path.lower()).suffix
    if (not kind.startswith(("audio/", "video/"))
            and kind not in ("application/octet-stream", "binary/octet-stream")
            and kind not in ("application/ogg", "application/x-ogg")):
        raise AudioError("O link direto precisa retornar um arquivo de áudio ou vídeo.", "not_media", 422)
    if extension not in DIRECT_EXTENSIONS:
        raise AudioError("O endereço redirecionou para um formato de arquivo não suportado.", "not_media", 422)


def inspect_media(url: str, guard: Guard, cookies: str | None = None, media_type: str = "audio"):
    cookiejar = None
    try:
        source = source_for(url)
        cookiejar = parse_netscape(cookies, url, direct_media=source == "Arquivo direto")
        public_url(url)
        guard.check()
        if source == "Arquivo direto":
            with direct_handler(guard) as handler:
                try:
                    response = handler.send(Request(url, method="HEAD"))
                except Exception as exc:
                    if "405" not in str(exc):
                        raise
                    response = handler.send(Request(url, headers={"Range": "bytes=0-0"}))
                with response:
                    verify_media_response(response, response.url)
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > MAX_SOURCE:
                        raise AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)
            return {"title": direct_title(url), "duration": None, "thumbnail": None,
                    "source": source, "webpage_url": url}
        with SafeYoutubeDL(options(guard), guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
        if not info:
            raise AudioError("Não foi possível identificar esse áudio.")
        return metadata(info, url, source)
    except Exception as exc:
        raise translate_error(exc, guard) from exc
    finally:
        if cookiejar is not None:
            cookiejar.clear()


def direct_download(url: str, directory: Path, guard: Guard):
    source = directory / "source.media"
    with direct_handler(guard) as handler, handler.send(Request(url)) as response:
        verify_media_response(response, response.url)
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > guard.maximum_bytes:
            raise AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)
        with source.open("wb") as output:
            while chunk := response.read(64 * 1024):
                output.write(chunk)
    return source, {"title": direct_title(url), "duration": None, "thumbnail": None,
                    "source": "Arquivo direto", "webpage_url": url}


def acquire_media(url: str, directory: Path, guard: Guard, audio_format: str, cookies: str | None = None):
    source_name = source_for(url)
    cookiejar = parse_netscape(cookies, url, direct_media=source_name == "Arquivo direto")
    try:
        public_url(url)
        if source_name == "Arquivo direto":
            return direct_download(url, directory, guard)
        with SafeYoutubeDL(options(guard, directory), guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
            if not info:
                raise AudioError("Não foi possível identificar esse áudio.")
            details = metadata(info, url, source_name)
            # Force only native downloaders. FFmpeg must never receive an internet URL.
            formats = info.get("formats") or [info]
            info["formats"] = [item for item in formats if item.get("protocol") in
                               ("http", "https", "m3u8_native", "http_dash_segments") and not item.get("has_drm")]
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


def select_video_formats(info: dict, resolution: str, available_bytes: int, mute=False):
    """Select native streams ourselves; yt-dlp never gets a merge request."""
    safe = [item for item in info.get("formats", [info]) if item.get("protocol") in NATIVE_PROTOCOLS
            and not item.get("has_drm") and item.get("url")]
    audio = [item for item in safe if item.get("vcodec") == "none" and item.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda item: (item.get("abr") or item.get("tbr") or 0, item.get("asr") or 0), default=None)
    videos = [item for item in safe if item.get("vcodec") not in (None, "none")]
    if not videos:
        raise AudioError("Esse link não contém uma faixa de vídeo disponível.", "no_video", 422)
    def resolution_edge(item):
        width, height = item.get("width"), item.get("height")
        return min(width, height) if width and height else height or width or 0

    def fits_dimensions(item):
        width, height = item.get("width"), item.get("height")
        return (resolution_edge(item) <= 1080 and
                (not width or not height or width * height <= 1920 * 1080 and max(width, height) <= 1920))

    cap = 1080 if resolution == "source" else int(resolution)
    bounded_videos = [item for item in videos if fits_dimensions(item)]
    if not bounded_videos:
        raise AudioError("A origem só oferece vídeo acima de 1080p. Use outro link ou uma origem com resolução menor.", "unavailable_resolution", 422)
    # Size estimates reserve a little room for metadata/manifests. When needed,
    # choose a smaller source before spending the shared transfer budget.
    caps = list(dict.fromkeys([cap] + [level for level in (720, 480, 360) if level < cap]))
    for height in caps:
        candidates = [item for item in bounded_videos if resolution_edge(item) <= height]
        candidates.sort(key=lambda item: (resolution_edge(item), (item.get("width") or 0) * (item.get("height") or 0),
                                          item.get("vcodec") != "none" and item.get("acodec") == "none",
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
    # The requested resolution describes the output. A single 720p/1080p
    # source can still be downloaded safely and reduced to 360p/480p locally.
    for video in sorted(bounded_videos, key=lambda item: (resolution_edge(item), item.get("tbr") or 0)):
        separate_audio = None if mute or video.get("acodec") not in (None, "none") else best_audio
        sizes = [estimated_size(video, info.get("duration"))]
        if separate_audio:
            sizes.append(estimated_size(separate_audio, info.get("duration")))
        if all(value is not None for value in sizes) and sum(sizes) > available_bytes * .96:
            continue
        return video, separate_audio
    raise AudioError("A origem do vídeo ultrapassa 128 MB, mesmo em uma resolução menor. Use um vídeo menor.", "source_too_large", 413)


def acquire_video_media(url: str, directory: Path, guard: Guard, video_resolution="source", cookies=None, mute=False):
    source_name = source_for(url)
    cookiejar = parse_netscape(cookies, url, direct_media=source_name == "Arquivo direto")
    try:
        public_url(url)
        if source_name == "Arquivo direto":
            path, details = direct_download(url, directory, guard)
            return VideoSources(path), details
        opts = options(guard, directory)
        # Extraction lists every format. Downloads below receive individual
        # streams, not the bestvideo+bestaudio merger path.
        opts["format"] = "bestvideo/best/bestaudio"
        with SafeYoutubeDL(opts, guard, cookiejar=cookiejar) as downloader:
            info = downloader.extract_info(url, download=False)
            if not info:
                raise AudioError("Não foi possível identificar esse vídeo.", "no_video", 422)
            details = metadata(info, url, source_name)
            video, audio = select_video_formats(info, str(video_resolution), guard.maximum_bytes - guard.received, mute)
            video_path, audio_path = directory / "source-video.media", None
            for item, path in ((video, video_path), (audio, directory / "source-audio.media")):
                if item is None:
                    continue
                guard.check()
                # Exclude selected/merged format bookkeeping inherited from extraction.
                stream_info = {key: value for key, value in info.items() if key not in
                               ("formats", "requested_formats", "requested_downloads", "url", "protocol", "ext", "format_id")}
                stream_info.update(item)
                stream_info["http_headers"] = downloader._calc_headers(stream_info)
                success, _ = downloader.dl(str(path), stream_info)
                if not success or not path.is_file() or not path.stat().st_size:
                    raise AudioError("A origem não entregou o vídeo completo. Tente outra resolução ou origem.", "download_failed", 422)
                if item is audio:
                    audio_path = path
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
            "pixel_format": "yuv420p" if re.search(r",\s*yuv420p(?:[,(\s]|$)", video or "") else None,
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
    if inspected["width"] * inspected["height"] > 1920 * 1080 or max(inspected["width"], inspected["height"]) > 1920:
        raise AudioError("A fonte ultrapassa a resolução de 1080p suportada. Use uma origem com resolução menor.", "unavailable_resolution", 422)
    start, output_duration = validate_trim(settings, inspected["duration"])
    audio_inspected = inspected
    if sources.audio:
        audio_inspected = probe_media(sources.audio, guard)
        if not audio_inspected["audio"]:
            raise AudioError("A origem não entregou uma faixa de áudio válida.", "no_audio", 422)
        validate_trim(settings, audio_inspected["duration"])
    cap = 1080 if settings.video_resolution == "source" else int(settings.video_resolution)
    # Bounds never increase the source dimensions. H.264 requires even pixels.
    long_edge = (int(cap * 16 / 9) // 2) * 2
    # iw/ih are evaluated after FFmpeg's automatic display-matrix rotation.
    # Phone videos may be coded as landscape pixels but displayed as portrait.
    filters = (f"scale=w='if(gte(iw,ih),min(iw,{long_edge}),min(iw,{cap}))':"
               f"h='if(gte(iw,ih),min(ih,{cap}),min(ih,{long_edge}))':"
               "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1")
    fps = min(inspected["fps"] or 30, 30)
    video_codec, audio_codec, _ = VIDEO_FORMATS[video_format]
    compatible_video_codec = "vp9" if video_format == "webm" else "h264"
    compatible_audio_codec = "opus" if video_format == "webm" else "aac"
    # Copy only streams whose decoded presentation already meets the output
    # contract. Seeking, scaling, rotation and normalization retain the precise
    # encode path; copying would otherwise silently lose the requested changes.
    copy_video = (not start and output_duration is None
                  and inspected.get("video_codec") == compatible_video_codec
                  and inspected.get("pixel_format") == "yuv420p"
                  and inspected.get("fps") is not None and inspected["fps"] <= 30
                  and inspected["width"] % 2 == inspected["height"] % 2 == 0
                  and min(inspected["width"], inspected["height"]) <= cap
                  and max(inspected["width"], inspected["height"]) <= long_edge
                  and inspected.get("sample_aspect_ratio") in (None, (1, 1))
                  and not inspected.get("rotation"))
    copy_audio = (quality == "source" and not start and output_duration is None
                  and not settings.normalize_audio
                  and audio_inspected.get("audio_codec") == compatible_audio_codec)
    # Prefer encoding over a copy that is already known to exceed the output
    # budget. Count all source bytes conservatively, plus newly encoded audio.
    estimated_output = sources.video.stat().st_size
    if sources.audio and not settings.mute:
        estimated_output += sources.audio.stat().st_size
    if not settings.mute and not copy_audio and inspected["duration"]:
        estimated_output += inspected["duration"] * (192 if quality == "source" else int(quality)) * 1000 / 8
    if estimated_output > MAX_OUTPUT * .95:
        copy_video = False
    target = directory / f"video.{video_format}"
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", "-nostats", "-nostdin", "-y"]
    command += input_command(sources.video)
    if sources.audio and not settings.mute:
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
    else:
        video_bitrate = (max(64, min(3500, int(MAX_OUTPUT * .8 * 8 / effective_duration / 1000)
                                          - (0 if settings.mute else 320))) if effective_duration else 3500)
        command += ["-vf", filters, "-r", f"{fps:g}", "-c:v", video_codec, "-pix_fmt", "yuv420p",
                    "-b:v", f"{video_bitrate}k", "-threads", "1"]
        if video_codec == "libx264":
            command += ["-preset", "ultrafast", "-maxrate", f"{video_bitrate}k", "-bufsize", f"{video_bitrate * 2}k"]
        else:
            command += ["-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1"]
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
    return result


def prepare_download(url, directory, audio_format, quality, guard, cookies: str | None = None,
                     settings: MediaSettings | None = None):
    settings = settings or MediaSettings()
    try:
        if settings.media_type == "video":
            sources, details = acquire_video_media(url, directory, guard, settings.video_resolution,
                                                   cookies=cookies, mute=settings.mute)
            target = convert_video(sources, directory, audio_format, quality, guard, settings)
            inputs = ([sources.video, sources.audio] if isinstance(sources, VideoSources) else [sources])
            for path in inputs:
                if path:
                    path.unlink(missing_ok=True)
            return target, details
        source, details = acquire_media(url, directory, guard, audio_format, cookies=cookies)
        if settings == MediaSettings():
            target = convert_audio(source, directory, audio_format, quality, guard)
        else:
            target = convert_audio(source, directory, audio_format, quality, guard, settings=settings)
        source.unlink(missing_ok=True)
        return target, details
    except Exception as exc:
        raise translate_error(exc, guard) from exc
