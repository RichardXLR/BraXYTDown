from pathlib import Path

from baixatube.models import FormatOption, MediaInfo
from baixatube.utils import (
    estimate_output_bytes,
    format_bytes,
    format_duration,
    has_free_space,
    is_supported_url,
    preview_output_name,
    safe_filename,
)


def test_supported_youtube_urls():
    assert is_supported_url("https://www.youtube.com/watch?v=abc123")
    assert is_supported_url("https://youtu.be/abc123")
    assert is_supported_url("https://music.youtube.com/watch?v=abc123")


def test_rejects_lookalike_and_non_http_urls():
    assert not is_supported_url("https://youtube.com.example.test/watch?v=x")
    assert not is_supported_url("javascript:alert(1)")
    assert not is_supported_url("not a url")


def test_safe_filename_and_duration():
    assert safe_filename('  vídeo: "teste"?  ') == "vídeo_ _teste__"
    assert safe_filename("...") == "video"
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"


def test_free_space_for_small_file(tmp_path: Path):
    assert has_free_space(tmp_path, 1, reserve=0)


def test_output_preview_and_size_estimate_are_stable():
    media = MediaInfo(
        "abc",
        "Vídeo: teste",
        "https://youtu.be/abc",
        duration=60,
        formats=[FormatOption("1", "720p", "mp4", 720, 10_000_000)],
    )

    assert estimate_output_bytes(media, "video", "mp4", "720") > 10_000_000
    assert format_bytes(10 * 1024 * 1024) == "10.0 MB"
    assert preview_output_name(media.title, media.id, "mp4", "%(title).180B [%(id)s].%(ext)s") == "Vídeo_ teste [abc].mp4"
