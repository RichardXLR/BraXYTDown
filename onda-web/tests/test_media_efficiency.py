"""No duration ceiling; compatible media takes the real FFmpeg remux path."""
from pathlib import Path
import subprocess

import imageio_ffmpeg
import pytest

from api import engine, index, security


def ffmpeg(*arguments, text=False):
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *map(str, arguments)],
                          capture_output=True, text=text, timeout=40, check=True)


def elementary_stream(path, kind):
    muxer = "h264" if kind == "v" else "adts"
    return ffmpeg("-i", path, "-map", f"0:{kind}:0", "-c", "copy", "-f", muxer, "-").stdout


@pytest.fixture
def tagged_video(tmp_path):
    source = tmp_path / "original.mp4"
    source.write_bytes((Path(__file__).resolve().parents[1] / "public" / "canary.mp4").read_bytes())
    return source


@pytest.mark.parametrize("extension", ["mp4", "mkv", "mov"])
def test_compatible_video_and_source_audio_preserve_encoded_packets_and_strip_tags(tagged_video, tmp_path, extension):
    target = engine.convert_video(tagged_video, tmp_path, extension, "source", security.Guard(),
                                  engine.MediaSettings(media_type="video"))
    # Elementary streams normalize container framing, so equality verifies the
    # actual encoded frames and AAC packets survived without a generation loss.
    assert elementary_stream(target, "v") == elementary_stream(tagged_video, "v")
    assert elementary_stream(target, "a") == elementary_stream(tagged_video, "a")
    metadata = ffmpeg("-i", target, "-f", "ffmetadata", "-", text=True)
    assert "ONDA_CANARY_PRIVATE" not in metadata.stdout + metadata.stderr
    assert engine.probe_media(target, security.Guard())["duration"] >= 2


def test_video_remux_can_mute_and_preserve_metadata_on_request(tagged_video, tmp_path):
    target = engine.convert_video(tagged_video, tmp_path, "mp4", "source", security.Guard(),
                                  engine.MediaSettings(media_type="video", mute=True, strip_metadata=False))
    assert elementary_stream(target, "v") == elementary_stream(tagged_video, "v")
    assert engine.probe_media(target, security.Guard())["audio"] is False
    metadata = ffmpeg("-i", target, "-f", "ffmetadata", "-", text=True)
    assert "ONDA_CANARY_PRIVATE_TITLE" in metadata.stdout


def test_explicit_audio_bitrate_encodes_audio_while_preserving_video(tagged_video, tmp_path):
    target = engine.convert_video(tagged_video, tmp_path, "mp4", 128, security.Guard(),
                                  engine.MediaSettings(media_type="video"))
    assert elementary_stream(target, "v") == elementary_stream(tagged_video, "v")
    assert elementary_stream(target, "a") != elementary_stream(tagged_video, "a")
    inspected = ffmpeg("-i", target, "-f", "ffmetadata", "-", text=True)
    assert "48000 Hz, stereo" in inspected.stderr
    assert engine.probe_media(target, security.Guard())["audio"]


def test_precise_trim_reencodes_video_instead_of_copying_unaligned_keyframes(tagged_video, tmp_path):
    target = engine.convert_video(tagged_video, tmp_path, "mp4", "source", security.Guard(),
                                  engine.MediaSettings(media_type="video", trim_start=.2, trim_end=.8))
    assert elementary_stream(target, "v") != elementary_stream(tagged_video, "v")
    assert .55 <= engine.probe_media(target, security.Guard())["duration"] <= .7


def test_video_longer_than_previous_cap_downloads_to_eof_without_truncation(tmp_path):
    source = tmp_path / "long.mp4"
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=32x32:rate=1", "-t", "1301",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", source)
    target = engine.convert_video(source, tmp_path, "mp4", "source", security.Guard(),
                                  engine.MediaSettings(media_type="video", mute=True))
    assert engine.probe_media(target, security.Guard())["duration"] == 1301
    assert elementary_stream(target, "v") == elementary_stream(source, "v")


def test_lossless_audio_longer_than_previous_cap_preserves_original_samples(tmp_path):
    source = tmp_path / "long.flac"
    ffmpeg("-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "1301",
           "-c:a", "flac", "-threads", "1", "-metadata", "artist=private-long-source", source)
    target = engine.convert_audio(source, tmp_path, "flac", "source", security.Guard())
    inspected = engine.probe_media(target, security.Guard())
    assert inspected["duration"] == 1301
    original = ffmpeg("-i", source, "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "hash", "-").stdout
    downloaded = ffmpeg("-i", target, "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "hash", "-").stdout
    assert downloaded == original
    metadata = ffmpeg("-i", target, "-f", "ffmetadata", "-", text=True)
    assert "private-long-source" not in metadata.stdout + metadata.stderr
    assert "8000 Hz, mono" in metadata.stderr


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_trim_schema_accepts_positions_past_old_duration_limits(kind):
    body = index.DownloadInput(url="https://cdn.example.com/long.mp4", media_type=kind,
                               format="mp4" if kind == "video" else "mp3", trim_start=3600, trim_end=7200)
    assert body.trim_start == 3600 and body.trim_end == 7200


@pytest.mark.parametrize("duration", [None, 1301, 86_400])
def test_inspection_accepts_unknown_or_long_finite_duration(duration):
    result = engine.metadata({"title": "Long media", "duration": duration},
                             "https://cdn.example.com/long.mp4", "Arquivo direto")
    assert result["duration"] == duration


def test_output_size_budget_still_applies_after_removing_duration_cap(tagged_video, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MAX_OUTPUT", 1000)
    with pytest.raises(security.AudioError) as raised:
        engine.convert_video(tagged_video, tmp_path, "mp4", "source", security.Guard(),
                             engine.MediaSettings(media_type="video"))
    assert raised.value.code == "output_too_large"


def test_unknown_duration_reads_finite_media_to_eof(tagged_video, tmp_path, monkeypatch):
    original_probe = engine.probe_media
    def unknown_input_duration(path, guard):
        result = original_probe(path, guard)
        if path == tagged_video:
            result["duration"] = None
        return result
    monkeypatch.setattr(engine, "probe_media", unknown_input_duration)
    target = engine.convert_video(tagged_video, tmp_path, "mp4", "source", security.Guard(),
                                  engine.MediaSettings(media_type="video"))
    assert original_probe(target, security.Guard())["duration"] >= 2
    assert elementary_stream(target, "v") == elementary_stream(tagged_video, "v")
