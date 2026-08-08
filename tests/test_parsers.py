import json

import pytest

from baixatube.models import DownloadStatus
from baixatube.parsers import parse_media_json, parse_progress_line


def test_parse_video_fixture():
    payload = json.dumps({
        "id": "abc", "title": "Demo", "webpage_url": "https://youtu.be/abc",
        "channel": "Canal", "duration": 62, "thumbnail": "https://example.test/a.jpg",
        "formats": [
            {"format_id": "1", "ext": "mp4", "height": 720, "vcodec": "avc1", "filesize": 100},
            {"format_id": "2", "ext": "m4a", "vcodec": "none"},
        ],
        "subtitles": {"pt": []}, "automatic_captions": {"en": []},
        "chapters": [
            {"title": "Abertura", "start_time": 0, "end_time": 12.5},
            {"title": "Final", "start_time": 12.5, "end_time": 62},
        ],
    })
    media = parse_media_json(payload)
    assert media.title == "Demo"
    assert media.formats[0].height == 720
    assert media.subtitles == ["en", "pt"]
    assert [chapter.title for chapter in media.chapters] == ["Abertura", "Final"]
    assert media.chapters[0].end_time == 12.5


def test_parse_playlist_fixture():
    media = parse_media_json(json.dumps({"id": "pl", "title": "Lista", "_type": "playlist", "entries": [{"id": "a", "title": "A", "duration": 10, "playlist_index": 7}]}))
    assert media.is_playlist
    assert media.entries[0].url.endswith("v=a")
    assert media.entries[0].playlist_index == 7


def test_parse_progress_and_conversion():
    progress = parse_progress_line("BT_PROGRESS| 42.5%|1.2MiB/s|00:05|4MiB|10MiB")
    assert progress and progress.status == DownloadStatus.DOWNLOADING
    assert progress.percent == 42.5
    assert parse_progress_line("[Merger] Merging formats").status == DownloadStatus.CONVERTING
    assert parse_progress_line("BT_FILE:C:/Downloads/file.mp4").status == DownloadStatus.COMPLETED


def test_formats_are_deduplicated_sorted_and_audio_only_is_ignored():
    media = parse_media_json(json.dumps({
        "id": "abc",
        "formats": [
            {"format_id": "audio", "ext": "m4a", "vcodec": "none"},
            {"format_id": "360-a", "ext": "mp4", "height": 360, "vcodec": "avc1"},
            {"format_id": "1080", "ext": "webm", "height": 1080, "vcodec": "vp9"},
            {"format_id": "360-b", "ext": "mp4", "height": 360, "vcodec": "avc1"},
        ],
    }))
    assert [(item.height, item.extension) for item in media.formats] == [(1080, "webm"), (360, "mp4")]


def test_playlist_skips_null_entries_and_builds_safe_fallbacks():
    media = parse_media_json(json.dumps({
        "_type": "playlist",
        "entries": [None, {"id": "xyz"}, {"id": "", "title": "Sem URL"}],
    }))
    assert len(media.entries) == 2
    assert media.entries[0].title == "Sem título"
    assert media.entries[0].url == "https://www.youtube.com/watch?v=xyz"
    assert media.entries[1].url == ""


@pytest.mark.parametrize("line", [
    "BT_PROGRESS| 42,75%|2MiB/s|3|1MiB|4MiB",
    "BT_PROGRESS|unknown|N/A|N/A||",
])
def test_progress_parser_accepts_localized_or_missing_numbers(line):
    progress = parse_progress_line(line)
    assert progress is not None
    assert progress.status == DownloadStatus.DOWNLOADING
    assert 0 <= progress.percent <= 42.75


def test_parser_ignores_noise_and_rejects_invalid_json():
    assert parse_progress_line("[download] Destination: demo.mp4") is None
    with pytest.raises(json.JSONDecodeError):
        parse_media_json("not-json")
