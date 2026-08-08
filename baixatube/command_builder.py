from __future__ import annotations

from .cookie_auth import cookie_arguments
from .models import DownloadRequest
from .plugin_security import active_plugin_dir


SAFE_TEMPLATE_FIELDS = {
    "id", "title", "uploader", "channel", "upload_date", "playlist_title",
    "playlist_index", "resolution", "height", "width", "ext", "section_title", "section_number",
}


def normalize_template(template: str) -> str:
    """Accept yt-dlp fields but reject path traversal and arbitrary nesting."""
    value = (template or "").strip()
    if not value or "/" in value or "\\" in value or ".." in value:
        return "%(title).180B [%(id)s].%(ext)s"
    import re
    fields = set(re.findall(r"%\(([^).]+)(?:\.[^)]+)?\)[#0 +\-\d.]*[a-zA-Z]", value))
    if not fields or not fields.issubset(SAFE_TEMPLATE_FIELDS) or "ext" not in fields:
        return "%(title).180B [%(id)s].%(ext)s"
    return value[:240]


def normalize_rate_limit(value: str) -> str:
    import re
    candidate = value.strip().upper()
    return candidate if re.fullmatch(r"\d+(?:\.\d+)?[KMG]?", candidate) else ""


def normalize_time(value: str) -> str:
    import re
    candidate = value.strip()
    if not candidate:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", candidate):
        return candidate
    return candidate if re.fullmatch(r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?", candidate) else ""


def normalize_custom_filename(value: str) -> str:
    import re

    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip()).replace("..", "_").strip(" .")
    return candidate[:180]


def build_analysis_args(
    url: str,
    *,
    po_token_provider: bool = False,
    cookie_mode: str = "off",
    cookie_browser: str = "edge",
    cookie_profile: str = "",
    cookie_file: str = "",
    cookie_consent: bool = False,
) -> list[str]:
    args = [
        "--ignore-config",
        "--dump-single-json",
        "--flat-playlist",
        "--no-warnings",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "--extractor-retries",
        "3",
    ]
    plugin = active_plugin_dir(po_token_provider)
    if plugin:
        args += ["--plugin-dirs", str(plugin), "--extractor-args", "youtube:player_client=mweb;fetch_pot=auto"]
    args += cookie_arguments(
        mode=cookie_mode,
        browser=cookie_browser,
        profile=cookie_profile,
        file_path=cookie_file,
        consent=cookie_consent,
    )
    args.append(url)
    return args


def build_download_args(request: DownloadRequest) -> list[str]:
    template = normalize_template(request.filename_template)
    custom_name = normalize_custom_filename(request.custom_filename)
    if custom_name:
        template = f"{custom_name}.%(ext)s"
    elif request.selected_chapters and "section_number" not in template:
        template = template.removesuffix(".%(ext)s") + " [%(section_number)03d - %(section_title)s].%(ext)s"
    args = [
        "--ignore-config",
        "--newline",
        "--progress",
        "--progress-template",
        "download:BT_PROGRESS|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str)s",
        "--print",
        "after_move:BT_FILE:%(filepath)s",
        "--continue",
        "--part",
        "--windows-filenames",
        "--trim-filenames",
        "180",
        "--output",
        template,
        "--paths",
        request.destination,
        "--retries",
        str(max(0, min(50, request.retries))),
        "--fragment-retries",
        str(max(0, min(50, request.retries))),
        "--file-access-retries",
        "5",
        "--retry-sleep",
        "exp=1:20",
        "--socket-timeout",
        "20",
        "--concurrent-fragments",
        str(max(1, min(16, request.concurrent_fragments))),
    ]
    plugin = active_plugin_dir(request.po_token_provider)
    if plugin:
        args += ["--plugin-dirs", str(plugin), "--extractor-args", "youtube:player_client=mweb;fetch_pot=auto"]
    args += cookie_arguments(
        mode=request.cookie_mode,
        browser=request.cookie_browser,
        profile=request.cookie_profile,
        file_path=request.cookie_file,
        consent=request.cookie_consent,
    )
    args.append("--force-overwrites" if request.overwrite else "--no-overwrites")
    rate_limit = normalize_rate_limit(request.rate_limit)
    if rate_limit:
        args += ["--limit-rate", rate_limit]
    if request.embed_metadata:
        args.append("--embed-metadata")
    if request.split_chapters and not request.selected_chapters:
        args.append("--split-chapters")
    if request.embed_thumbnail:
        args.append("--embed-thumbnail")
    if request.download_subtitles:
        languages = request.subtitle_languages.strip()[:120] or "pt.*,en.*"
        args += ["--write-subs", "--write-auto-subs", "--sub-langs", languages]
        if request.embed_subtitles and request.media_type == "video":
            args.append("--embed-subs")
    if request.write_description:
        args.append("--write-description")
    if request.write_info_json:
        args.append("--write-info-json")
    if request.sponsorblock:
        args += ["--sponsorblock-remove", "sponsor,selfpromo,interaction,intro,outro,preview"]
    if request.playlist_items:
        args += ["--yes-playlist", "--playlist-items", ",".join(request.playlist_items)]
    else:
        args.append("--no-playlist")
    if request.playlist_reverse:
        args.append("--playlist-reverse")
    selected_sections = []
    for chapter in request.selected_chapters[:200]:
        if not isinstance(chapter, dict):
            continue
        start_value = normalize_time(str(chapter.get("start", "")))
        end_value = normalize_time(str(chapter.get("end", "")))
        if start_value:
            selected_sections.append(f"*{start_value}-{end_value or 'inf'}")
    start, end = normalize_time(request.trim_start), normalize_time(request.trim_end)
    if selected_sections:
        for section in selected_sections:
            args += ["--download-sections", section]
        args.append("--force-keyframes-at-cuts")
    elif start or end:
        args += ["--download-sections", f"*{start or '0'}-{end or 'inf'}", "--force-keyframes-at-cuts"]

    if request.media_type == "audio":
        codec = {"m4a": "m4a", "opus": "opus", "wav": "wav", "mp3": "mp3"}.get(request.output_format, "mp3")
        args += ["-f", "bestaudio/best", "--extract-audio", "--audio-format", codec]
        if codec in {"mp3", "m4a", "opus"}:
            quality = request.audio_quality if request.audio_quality in {"0", "128K", "192K", "256K", "320K"} else "0"
            args += ["--audio-quality", quality]
    else:
        max_height = request.quality if request.quality.isdigit() else None
        video_ext, audio_ext = ("mp4", "m4a") if request.output_format == "mp4" else ("webm", "webm")
        height_filter = f"[height<={max_height}]" if max_height else ""
        # Keep the chosen container truthful.  A generic fallback could select
        # H.264/AAC for WebM (or WebM codecs for MP4) and fail only after a long
        # download during merge.
        selector = f"bv[ext={video_ext}]{height_filter}+ba[ext={audio_ext}]/b[ext={video_ext}]{height_filter}"
        args += ["-f", selector, "--merge-output-format", request.output_format]
    args.append(request.url)
    return args
