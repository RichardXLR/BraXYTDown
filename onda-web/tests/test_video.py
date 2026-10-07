"""Exercise video downloads with local fixtures and the real FFmpeg binary."""
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient

from api import engine, index, security


def ffmpeg(*arguments):
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *map(str, arguments)],
        capture_output=True, text=True, timeout=30, check=True,
    )


@pytest.fixture(scope="module")
def video_bytes(tmp_path_factory):
    directory = tmp_path_factory.mktemp("video-fixture")
    source = directory / "tagged.mp4"
    chapters = directory / "chapters.ffmetadata"
    chapters.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1800\n"
        "title=private-fixture-chapter\n", encoding="utf-8",
    )
    ffmpeg(
        "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-f", "ffmetadata", "-i", chapters,
        "-map", "0:v:0", "-map", "1:a:0", "-map_chapters", "2",
        "-t", "1.8", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-af", "volume=0.1",
        "-metadata", "title=private-fixture-title",
        "-metadata", "artist=private-fixture-artist",
        "-metadata", "comment=private-fixture-comment",
        "-metadata:s:v:0", "handler_name=private-fixture-video-handler",
        "-metadata:s:a:0", "handler_name=private-fixture-audio-handler", source,
    )
    return source.read_bytes()


@pytest.fixture
def video_client(monkeypatch, tmp_path, video_bytes):
    def local_media(_url, directory, guard, video_resolution="source", cookies=None, mute=False):
        guard.check()
        source = directory / "source.mp4"
        source.write_bytes(video_bytes)
        return source, {
            "title": "Vídeo de teste", "duration": 1.8, "thumbnail": None,
            "source": "YouTube", "webpage_url": _url,
        }

    monkeypatch.setattr(engine, "acquire_video_media", local_media)
    directories = []
    original_mkdtemp = tempfile.mkdtemp

    def create_temp(**kwargs):
        kwargs["dir"] = str(tmp_path)
        directory = original_mkdtemp(**kwargs)
        directories.append(Path(directory))
        return directory

    monkeypatch.setattr(index.tempfile, "mkdtemp", create_temp)
    with TestClient(index.app) as client:
        client.created_directories = directories
        yield client
    assert all(not path.exists() for path in directories)


def probe_download(response, tmp_path, extension):
    output = tmp_path / f"download.{extension}"
    output.write_bytes(response.content)
    inspected = ffmpeg("-i", output, "-f", "ffmetadata", "-")
    size = re.search(r"Video:.*?,\s+([1-9]\d*)x([1-9]\d*)(?=[,\s])", inspected.stderr)
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", inspected.stderr)
    assert size, inspected.stderr
    assert duration, inspected.stderr
    return {
        "path": output,
        "width": int(size[1]), "height": int(size[2]),
        "duration": int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]),
        "has_audio": bool(re.search(r"Stream #.*Audio:", inspected.stderr)),
        "metadata": inspected.stdout + inspected.stderr,
    }


def video_request(client, **options):
    return client.post("/api/download", json={
        "url": "https://www.youtube.com/watch?v=local-fixture",
        "media_type": "video", "format": "mp4", "video_resolution": "source",
        **options,
    })


@pytest.mark.parametrize("extension,signature,content_type", [
    ("mp4", b"ftyp", "video/mp4"),
    ("webm", b"\x1a\x45\xdf\xa3", "video/webm"),
    ("mkv", b"\x1a\x45\xdf\xa3", "video/x-matroska"),
    ("mov", b"ftyp", "video/quicktime"),
])
def test_real_video_conversion_formats_and_cleanup(video_client, tmp_path, extension, signature, content_type):
    response = video_request(video_client, format=extension)
    assert response.status_code == 200, response.text
    assert signature in response.content[:12]
    assert response.headers["content-type"].split(";")[0] == content_type
    assert int(response.headers["content-length"]) == len(response.content)
    assert f".{extension}" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    details = probe_download(response, tmp_path, extension)
    assert (details["width"], details["height"]) == (320, 180)
    assert details["has_audio"]
    assert 1.7 <= details["duration"] <= 2.0
    assert "private-fixture" not in details["metadata"]
    assert all(not path.exists() for path in video_client.created_directories)


def test_video_trim_mute_and_resolution_never_upscales(video_client, tmp_path):
    response = video_request(video_client, video_resolution="720", trim_start=0.4, trim_end=1.2, mute=True)
    assert response.status_code == 200, response.text
    details = probe_download(response, tmp_path, "mp4")
    assert (details["width"], details["height"]) == (320, 180)
    assert not details["has_audio"]
    assert 0.72 <= details["duration"] <= 0.9
    assert "private-fixture" not in details["metadata"]


def test_video_can_preserve_metadata_when_requested(video_client, tmp_path):
    response = video_request(video_client, strip_metadata=False)
    assert response.status_code == 200, response.text
    details = probe_download(response, tmp_path, "mp4")
    for tag in ("title", "artist", "comment"):
        assert f"private-fixture-{tag}" in details["metadata"]


def mean_volume(path):
    measured = ffmpeg("-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-")
    volume = re.search(r"mean_volume: (-?[\d.]+) dB", measured.stderr)
    assert volume, measured.stderr
    return float(volume[1])


def test_video_normalize_audio_changes_loudness(video_client, tmp_path, video_bytes):
    source = tmp_path / "original.mp4"
    source.write_bytes(video_bytes)
    response = video_request(video_client, normalize_audio=True)
    assert response.status_code == 200, response.text
    details = probe_download(response, tmp_path, "mp4")
    assert details["has_audio"]
    assert mean_volume(details["path"]) > mean_volume(source) + 3


@pytest.mark.parametrize("options", [
    {"format": "mp3"},
    {"video_resolution": "2160"},
    {"trim_start": -1},
    {"trim_start": 1.2, "trim_end": 0.4},
    {"trim_start": 0.4, "trim_end": 0.4},
])
def test_video_rejects_invalid_options_before_acquiring_media(video_client, options):
    response = video_request(video_client, **options)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert not video_client.created_directories


@pytest.fixture
def native_streams(tmp_path, video_bytes):
    combined = tmp_path / "combined.mp4"
    combined.write_bytes(video_bytes)
    video, audio = tmp_path / "native-video.mp4", tmp_path / "native-audio.m4a"
    ffmpeg("-y", "-i", combined, "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy", "-map_metadata", "-1", video)
    ffmpeg("-y", "-i", combined, "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy", "-map_metadata", "-1", audio)
    return video.read_bytes(), audio.read_bytes()


def mock_native_upstream(monkeypatch, native_streams, audio_url="https://cdn.example.com/audio.m4a"):
    import io
    from yt_dlp.networking import Response
    from yt_dlp.networking._urllib import UrllibRH
    video_bytes, audio_bytes = native_streams
    requested = []
    def extracted(_self, url, download=False):
        return {"id": "fixture", "title": "Vídeo nativo", "duration": 1.8,
            "extractor": "youtube", "extractor_key": "Youtube", "webpage_url": url,
            "formats": [
                {"format_id": "video", "url": "https://cdn.example.com/video.mp4", "ext": "mp4",
                 "vcodec": "h264", "acodec": "none", "height": 180, "width": 320, "protocol": "https"},
                {"format_id": "audio", "url": audio_url, "ext": "m4a", "vcodec": "none",
                 "acodec": "aac", "protocol": "https"},
            ]}
    def fake_http(_self, request):
        requested.append(request.url)
        data = video_bytes if "video.mp4" in request.url else audio_bytes
        return Response(io.BytesIO(data), request.url, {"Content-Type": "video/mp4", "Content-Length": str(len(data))})
    monkeypatch.setattr(engine.SafeYoutubeDL, "extract_info", extracted)
    monkeypatch.setattr(UrllibRH, "_send", fake_http)
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    return requested


def test_native_separate_video_audio_downloads_and_merges_only_local_files(monkeypatch, tmp_path, native_streams):
    requested = mock_native_upstream(monkeypatch, native_streams)
    with TestClient(index.app) as client:
        response = video_request(client)
    assert response.status_code == 200, response.text
    assert requested == ["https://cdn.example.com/video.mp4", "https://cdn.example.com/audio.m4a"]
    output = probe_download(response, tmp_path, "mp4")
    assert output["has_audio"]
    assert (output["width"], output["height"]) == (320, 180)


def test_separate_streams_share_one_aggregate_transfer_budget(monkeypatch, tmp_path, native_streams):
    requested = mock_native_upstream(monkeypatch, native_streams)
    directory = tmp_path / "job"
    directory.mkdir()
    guard = security.Guard(maximum_bytes=sum(map(len, native_streams)) - 1)
    with pytest.raises(security.AudioError) as raised:
        engine.prepare_download("https://www.youtube.com/watch?v=BaW_jenozKc", directory, "mp4", 192,
                                guard, settings=engine.MediaSettings(media_type="video"))
    assert raised.value.code == "source_too_large"
    assert len(requested) == 2
    assert guard.received > guard.maximum_bytes


def test_secondary_stream_cannot_target_private_network(monkeypatch, native_streams):
    requested = mock_native_upstream(monkeypatch, native_streams, audio_url="http://127.0.0.1/private.m4a")
    with TestClient(index.app) as client:
        response = video_request(client)
    assert response.status_code == 400
    assert response.json()["code"] == "unsafe_url"
    assert requested == ["https://cdn.example.com/video.mp4"]


def test_single_high_resolution_source_can_be_reduced_to_requested_360p(tmp_path):
    source = tmp_path / "source.mp4"
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=1920x1080:rate=10", "-t", "0.4",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", source)
    video_format = {"format_id": "only", "url": "https://cdn.example.com/only.mp4", "ext": "mp4",
                    "protocol": "https", "vcodec": "h264", "acodec": "none", "height": 1080,
                    "width": 1920, "filesize": source.stat().st_size}
    selected, _ = engine.select_video_formats({"duration": .4, "formats": [video_format]}, "360", engine.MAX_SOURCE, mute=True)
    assert selected is video_format
    output = engine.convert_video(source, tmp_path, "mp4", 192, security.Guard(),
                                  engine.MediaSettings(media_type="video", video_resolution="360", mute=True))
    inspected = engine.probe_media(output, security.Guard())
    assert (inspected["width"], inspected["height"]) == (640, 360)


def test_source_estimate_uses_720p_when_1080p_exceeds_budget():
    common = {"protocol": "https", "vcodec": "h264", "acodec": "aac", "url": "https://cdn.example.com/v.mp4"}
    too_big = dict(common, height=1080, filesize=engine.MAX_SOURCE + 1)
    fits = dict(common, height=720, filesize=2 * 1024 * 1024)
    selected, _ = engine.select_video_formats({"duration": 30, "formats": [too_big, fits]}, "source", engine.MAX_SOURCE)
    assert selected is fits


def test_metadata_text_cannot_spoof_video_stream_selection(tmp_path, video_bytes):
    original, spoof = tmp_path / "original.mp4", tmp_path / "spoof.mp4"
    original.write_bytes(video_bytes)
    ffmpeg("-y", "-i", original, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
           "-metadata", "comment=Stream #0:1: Video: h264, yuv420p, 320x180, 30 fps", spoof)
    inspected = engine.probe_media(spoof, security.Guard())
    assert inspected["video_index"] == 0
    output = engine.convert_video(spoof, tmp_path, "mp4", 192, security.Guard(), engine.MediaSettings(media_type="video"))
    assert engine.probe_media(output, security.Guard())["video"]


def test_audio_only_metadata_cannot_create_fake_video_stream(tmp_path, video_bytes):
    original, spoof = tmp_path / "original.mp4", tmp_path / "spoof.m4a"
    original.write_bytes(video_bytes)
    ffmpeg("-y", "-i", original, "-map", "0:a:0", "-vn", "-c:a", "copy",
           "-metadata", "comment=Stream #0:0: Video: h264, yuv420p, 320x180, 30 fps", spoof)
    assert not engine.probe_media(spoof, security.Guard())["video"]
    with pytest.raises(security.AudioError) as raised:
        engine.convert_video(spoof, tmp_path, "mp4", 192, security.Guard(), engine.MediaSettings(media_type="video"))
    assert raised.value.code == "no_video"


def test_short_trim_cannot_bypass_full_source_duration_with_spoofed_metadata(tmp_path):
    source = tmp_path / "long.mp4"
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=32x32:rate=1", "-t", "310",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
           "-metadata", "comment=Duration: 00:00:01.00", source)
    assert engine.probe_media(source, security.Guard())["duration"] == 310
    with pytest.raises(security.AudioError) as raised:
        engine.convert_video(source, tmp_path, "mp4", 192, security.Guard(),
                             engine.MediaSettings(media_type="video", trim_end=1, mute=True))
    assert raised.value.code == "too_long"


def test_cover_art_is_not_a_downloadable_video(tmp_path, video_bytes):
    original, cover, audio = tmp_path / "original.mp4", tmp_path / "cover.jpg", tmp_path / "covered.m4a"
    original.write_bytes(video_bytes)
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=32x32", "-frames:v", "1", "-threads", "1", cover)
    ffmpeg("-y", "-i", original, "-i", cover, "-map", "0:a:0", "-map", "1:v:0", "-c:a", "copy",
           "-c:v", "copy", "-disposition:v:0", "attached_pic", audio)
    assert not engine.probe_media(audio, security.Guard())["video"]
    with pytest.raises(security.AudioError) as raised:
        engine.convert_video(audio, tmp_path, "mp4", 192, security.Guard(), engine.MediaSettings(media_type="video"))
    assert raised.value.code == "no_video"


def test_audio_trimming_normalization_and_optional_metadata_preservation(tmp_path, video_bytes):
    source = tmp_path / "tagged.mp4"
    source.write_bytes(video_bytes)
    settings = engine.MediaSettings(trim_start=.4, trim_end=1.2, strip_metadata=False, normalize_audio=True)
    output = engine.convert_audio(source, tmp_path, "mp3", 192, security.Guard(), settings=settings)
    inspected = ffmpeg("-i", output, "-f", "ffmetadata", "-")
    assert "private-fixture-title" in inspected.stdout
    duration = engine.probe_media(output, security.Guard())["duration"]
    assert .75 <= duration <= .95
    assert mean_volume(output) > mean_volume(source) + 3


def test_video_health_and_default_format(video_client):
    health = video_client.get("/api/health").json()
    assert health["videoFormats"] == ["mp4", "webm", "mkv", "mov"]
    assert health["maxVideoDuration"] == 300
    response = video_client.post("/api/download", json={"url": "https://www.youtube.com/watch?v=BaW_jenozKc", "media_type": "video"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["x-media-title"] == response.headers["x-audio-title"]


def test_portrait_source_1080p_can_be_downloaded_and_reduced_to_360p(tmp_path):
    source = tmp_path / "portrait.mp4"
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=1080x1920:rate=10", "-t", "0.4",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", source)
    item = {"url": "https://cdn.example.com/portrait.mp4", "protocol": "https", "vcodec": "h264",
            "acodec": "none", "width": 1080, "height": 1920, "filesize": source.stat().st_size}
    selected, _ = engine.select_video_formats({"duration": .4, "formats": [item]}, "360", engine.MAX_SOURCE, mute=True)
    assert selected is item
    target = engine.convert_video(source, tmp_path, "mp4", 192, security.Guard(),
                                  engine.MediaSettings(media_type="video", video_resolution="360", mute=True))
    inspected = engine.probe_media(target, security.Guard())
    assert (inspected["width"], inspected["height"]) == (360, 640)


def test_odd_dimensions_are_reduced_to_even_and_frame_rate_capped(tmp_path):
    source = tmp_path / "odd.mp4"
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=321x181:rate=60,format=yuv444p", "-t", "0.2",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", source)
    target = engine.convert_video(source, tmp_path, "mp4", 192, security.Guard(),
                                  engine.MediaSettings(media_type="video", mute=True))
    inspected = engine.probe_media(target, security.Guard())
    assert inspected["width"] <= 321 and inspected["height"] <= 181
    assert inspected["width"] % 2 == inspected["height"] % 2 == 0
    assert inspected["fps"] <= 30


def test_large_decoded_dimensions_are_rejected_before_conversion(monkeypatch, tmp_path):
    source = tmp_path / "huge.mp4"
    source.write_bytes(b"small-container")
    monkeypatch.setattr(engine, "probe_media", lambda *_args: {"video": True, "width": 100000,
        "height": 100000, "audio": False, "duration": 1, "fps": 30, "video_index": 0})
    with pytest.raises(security.AudioError) as raised:
        engine.convert_video(source, tmp_path, "mp4", 192, security.Guard(), engine.MediaSettings(media_type="video"))
    assert raised.value.code == "unavailable_resolution"
    assert not (tmp_path / "video.mp4").exists()


def test_phone_rotation_is_applied_before_orientation_sensitive_scaling(tmp_path):
    coded, rotated = tmp_path / "coded.mp4", tmp_path / "phone.mov"
    ffmpeg("-y", "-f", "lavfi", "-i", "color=blue:size=1920x1080:rate=10", "-t", "0.4",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", coded)
    ffmpeg("-y", "-display_rotation:v:0", "90", "-i", coded, "-c", "copy", rotated)
    assert (engine.probe_media(rotated, security.Guard())["width"], engine.probe_media(rotated, security.Guard())["height"]) == (1920, 1080)
    target = engine.convert_video(rotated, tmp_path, "mp4", 192, security.Guard(),
                                  engine.MediaSettings(media_type="video", video_resolution="360", mute=True))
    inspected = engine.probe_media(target, security.Guard())
    assert (inspected["width"], inspected["height"]) == (360, 640)


def test_native_hls_like_transport_stream_converts_using_only_local_protocols(tmp_path, video_bytes):
    original, transport = tmp_path / "original.mp4", tmp_path / "native-segments.ts"
    original.write_bytes(video_bytes)
    ffmpeg("-y", "-i", original, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-sn", "-dn", "-f", "mpegts", transport)
    target = engine.convert_video(transport, tmp_path, "mp4", 192, security.Guard(), engine.MediaSettings(media_type="video"))
    inspected = engine.probe_media(target, security.Guard())
    assert inspected["video"] and inspected["audio"]
    assert (inspected["width"], inspected["height"]) == (320, 180)
    assert engine.source_for("https://cdn.example.com/native.ts") == "Arquivo direto"
    assert engine.source_for("https://cdn.example.com/native.m2ts") == "Arquivo direto"


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_unknown_whole_source_duration_is_rejected_even_for_short_trim(monkeypatch, tmp_path, kind):
    source = tmp_path / "unknown.media"
    source.write_bytes(b"unknown-duration-fixture")
    monkeypatch.setattr(engine, "probe_media", lambda *_args: {"video": True, "audio": True,
        "width": 320, "height": 180, "duration": None, "fps": 25, "video_index": 0})
    settings = engine.MediaSettings(media_type=kind, trim_end=1)
    with pytest.raises(security.AudioError) as raised:
        if kind == "video":
            engine.convert_video(source, tmp_path, "mp4", 192, security.Guard(), settings)
        else:
            engine.convert_audio(source, tmp_path, "mp3", 192, security.Guard(), settings=settings)
    assert raised.value.code == "duration_unknown"
    assert not list(tmp_path.glob("audio.*")) and not list(tmp_path.glob("video.*"))


@pytest.mark.parametrize("stride,offset", [(188, 0), (192, 4), (204, 0)])
@pytest.mark.parametrize("prefix", [b"", b"prefix-metadata-junk!"])
def test_transport_workaround_preserves_every_media_packet_and_removes_only_sdt(tmp_path, stride, offset, prefix):
    def packet(pid, marker):
        prefix = bytes([marker]) * offset
        payload = bytes([0x47, (pid >> 8) & 0x1f, pid & 0xff, 0x10]) + bytes([marker]) * 184
        return prefix + payload + bytes([marker]) * (stride - offset - 188)
    packets = [packet(0, 1), packet(0x11, 2), packet(0x1000, 3), packet(0x100, 4), packet(0x101, 5), packet(0x11, 6)]
    source = tmp_path / "transport.media"
    source.write_bytes(prefix + b"".join(packets))
    engine.sanitize_transport_metadata(source, security.Guard())
    assert source.read_bytes() == prefix + b"".join(packet for index, packet in enumerate(packets) if index not in (1, 5))
    assert not source.with_name(source.name + ".transport-safe").exists()


@pytest.mark.parametrize("m2ts", [False, True])
def test_transport_stream_audio_remains_convertible_after_runtime_workaround(tmp_path, video_bytes, m2ts):
    original, transport = tmp_path / "original.mp4", tmp_path / "native.ts"
    original.write_bytes(video_bytes)
    ffmpeg("-y", "-i", original, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-sn", "-dn", "-f", "mpegts", transport)
    if m2ts:
        packets = transport.read_bytes()
        wrapped = tmp_path / "native.m2ts"
        wrapped.write_bytes(b"".join(b"\x00\x00\x00\x00" + packets[start:start + 188] for start in range(0, len(packets), 188)))
        transport = wrapped
    output = engine.convert_audio(transport, tmp_path, "mp3", 192, security.Guard())
    inspected = engine.probe_media(output, security.Guard())
    assert inspected["audio"] and not inspected["video"]
    assert 1.7 <= inspected["duration"] <= 2
