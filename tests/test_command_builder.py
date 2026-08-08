from baixatube.command_builder import build_analysis_args, build_download_args
from baixatube.models import DownloadRequest


def request(**changes):
    values = dict(id="1", url="https://youtu.be/x", title="Teste", destination="C:/Downloads")
    values.update(changes)
    return DownloadRequest(**values)


def test_video_command_has_progress_destination_and_quality():
    args = build_download_args(request(quality="1080", output_format="mp4"))
    assert "--progress-template" in args
    assert "C:/Downloads" in args
    selector = args[args.index("-f") + 1]
    assert "height<=1080" in selector
    assert "ext=mp4" in selector
    assert "/bv*" not in selector
    assert args[-1] == "https://youtu.be/x"


def test_audio_command_and_optional_features():
    args = build_download_args(request(media_type="audio", output_format="mp3", download_subtitles=True))
    assert "--extract-audio" in args
    assert args[args.index("--audio-format") + 1] == "mp3"
    assert "--write-subs" in args


def test_playlist_selection():
    args = build_download_args(request(playlist_items=["1", "3-5"]))
    assert args[args.index("--playlist-items") + 1] == "1,3-5"
    assert "--yes-playlist" in args


def test_analysis_command_is_machine_readable_and_keeps_url_last():
    url = "https://www.youtube.com/watch?v=abc"
    args = build_analysis_args(url)
    assert args[-1] == url
    assert "--dump-single-json" in args
    assert "--flat-playlist" in args
    assert args[0] == "--ignore-config"
    assert "--socket-timeout" in args


def test_untrusted_template_and_rate_limit_are_sanitized():
    args = build_download_args(request(
        filename_template="../../%(title)s/%(ext)s",
        rate_limit="1M --exec calc.exe",
    ))
    output = args[args.index("--output") + 1]
    assert output == "%(title).180B [%(id)s].%(ext)s"
    assert "--limit-rate" not in args
    assert "calc.exe" not in args


def test_advanced_limits_are_clamped_and_config_is_ignored():
    args = build_download_args(request(
        retries=999,
        concurrent_fragments=0,
        rate_limit="2.5m",
        overwrite=True,
    ))
    assert args[0] == "--ignore-config"
    assert args[args.index("--retries") + 1] == "50"
    assert args[args.index("--fragment-retries") + 1] == "50"
    assert args[args.index("--concurrent-fragments") + 1] == "1"
    assert args[args.index("--limit-rate") + 1] == "2.5M"
    assert "--force-overwrites" in args
    assert "--no-overwrites" not in args


def test_audio_does_not_request_embedded_subtitles_and_quality_is_validated():
    args = build_download_args(request(
        media_type="audio",
        output_format="opus",
        download_subtitles=True,
        embed_subtitles=True,
        audio_quality="not-valid",
    ))
    assert "--write-subs" in args
    assert "--embed-subs" not in args
    assert args[args.index("--audio-quality") + 1] == "0"


def test_trim_range_requires_normalized_times():
    args = build_download_args(request(trim_start="01:02", trim_end="2:03:04.5"))
    assert args[args.index("--download-sections") + 1] == "*01:02-2:03:04.5"
    assert "--force-keyframes-at-cuts" in args

    invalid = build_download_args(request(trim_start="; shutdown", trim_end=""))
    assert "--download-sections" not in invalid


def test_chapter_split_is_forwarded_to_yt_dlp():
    args = build_download_args(request(split_chapters=True))

    assert "--split-chapters" in args


def test_individual_chapters_create_bounded_sections_and_unique_names():
    args = build_download_args(
        request(
            split_chapters=True,
            selected_chapters=[
                {"title": "Abertura", "start": "0", "end": "12.5"},
                {"title": "Final", "start": "50", "end": "75"},
            ],
        )
    )

    assert "--split-chapters" not in args
    assert args.count("--download-sections") == 2
    assert "*0-12.5" in args
    assert "*50-75" in args
    assert "section_number" in args[args.index("--output") + 1]


def test_custom_filename_is_sanitized_and_kept_inside_destination():
    args = build_download_args(request(custom_filename='Minha: música/..\\teste?'))

    template = args[args.index("--output") + 1]
    assert "/" not in template and "\\" not in template and ".." not in template
    assert template.endswith(".%(ext)s")


def test_analysis_can_use_browser_cookies_without_combining_sources():
    args = build_analysis_args(
        "https://www.youtube.com/watch?v=abc",
        cookie_mode="browser",
        cookie_browser="edge",
        cookie_profile="Default",
        cookie_consent=True,
    )

    assert args[args.index("--cookies-from-browser") + 1] == "edge:Default"
    assert "--cookies" not in args
    assert args[-1].startswith("https://")


def test_download_can_use_valid_netscape_cookie_file(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    args = build_download_args(
        request(cookie_mode="file", cookie_file=str(cookie_file), cookie_consent=True)
    )

    assert args[args.index("--cookies") + 1] == str(cookie_file)
    assert "--cookies-from-browser" not in args
