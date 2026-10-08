"""Short transport-stream cuts must decode video and audio after the first GOP."""
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient

from api import engine, index, security


def ffmpeg(*arguments, text=True):
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *map(str, arguments)],
        capture_output=True, text=text, timeout=30, check=True,
    )


@pytest.fixture(scope="module")
def transport_bytes():
    source = Path(__file__).resolve().parents[1] / "public" / "canary.ts"
    original = source.read_bytes()
    yield original
    # Probing transport streams sanitizes a local copy, never the public fixture.
    assert source.read_bytes() == original


@pytest.fixture(scope="module")
def separate_streams(tmp_path_factory, transport_bytes):
    directory = tmp_path_factory.mktemp("separate-transport-streams")
    source, video, audio = directory / "canary.ts", directory / "video.ts", directory / "audio.m4a"
    source.write_bytes(transport_bytes)
    engine.sanitize_transport_metadata(source, security.Guard())
    ffmpeg("-y", "-i", source, "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy",
           "-f", "mpegts", video)
    ffmpeg("-y", "-i", source, "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy",
           "-movflags", "+faststart", audio)
    return video.read_bytes(), audio.read_bytes()


@pytest.fixture
def transport_client(monkeypatch, tmp_path, transport_bytes, separate_streams):
    mode = {"separate": False}

    def details(url):
        return {"title": "Trecho de transporte", "duration": 2.02, "thumbnail": None,
                "source": "Arquivo direto", "webpage_url": url}

    def acquire_video(url, directory, guard, video_resolution="source", cookies=None, mute=False, video_format="mp4"):
        guard.check()
        video = directory / "source-video.ts"
        if mode["separate"]:
            video.write_bytes(separate_streams[0])
            audio = directory / "source-audio.m4a"
            audio.write_bytes(separate_streams[1])
            return engine.VideoSources(video, audio), details(url)
        video.write_bytes(transport_bytes)
        return video, details(url)

    def acquire_audio(url, directory, guard, audio_format, cookies=None):
        guard.check()
        source = directory / "source-audio.ts"
        source.write_bytes(transport_bytes)
        return source, details(url)

    monkeypatch.setattr(engine, "acquire_video_media", acquire_video)
    monkeypatch.setattr(engine, "acquire_media", acquire_audio)
    original_mkdtemp = tempfile.mkdtemp
    directories = []

    def create_directory(**kwargs):
        kwargs["dir"] = str(tmp_path)
        directory = Path(original_mkdtemp(**kwargs))
        directories.append(directory)
        return str(directory)

    monkeypatch.setattr(index.tempfile, "mkdtemp", create_directory)
    with TestClient(index.app) as client:
        client.source_mode = mode
        client.created_directories = directories
        yield client
    assert all(not directory.exists() for directory in directories)


def download(client, *, kind="video", extension="mp4", mute=False, normalize=True):
    response = client.post("/api/download", json={
        "url": "https://cdn.example.com/canary.ts", "media_type": kind, "format": extension,
        "trim_start": .2, "trim_end": .8, "normalize_audio": normalize, "mute": mute,
    })
    assert response.status_code == 200, response.text
    assert len(response.content) == int(response.headers["content-length"])
    assert all(not directory.exists() for directory in client.created_directories)
    return response


def verify_decoded_clip(response, path, *, video=True, audio=True):
    path.write_bytes(response.content)
    inspected = ffmpeg("-i", path, "-f", "ffmetadata", "-").stderr
    duration = re.search(r"^  Duration: (\d+):(\d+):([\d.]+)", inspected, re.M)
    assert duration, inspected
    seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
    assert .55 <= seconds <= .69
    assert bool(re.search(r"^  Stream #.*Video:", inspected, re.M)) == video
    assert bool(re.search(r"^  Stream #.*Audio:", inspected, re.M)) == audio
    if video:
        decoded = ffmpeg("-i", path, "-map", "0:v:0", "-an", "-sn", "-dn", "-pix_fmt", "rgb24",
                         "-fps_mode", "passthrough", "-f", "rawvideo", "-", text=False).stdout
        # The canary is 320×180 at 15 fps: a .6 s cut contains nine actual frames.
        assert len(decoded) == 9 * 320 * 180 * 3
    if audio:
        decoded = ffmpeg("-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "48000",
                         "-c:a", "pcm_s16le", "-f", "s16le", "-", text=False).stdout
        decoded_seconds = len(decoded) / (48000 * 2)
        # AAC and MP3 padding can add a few milliseconds to the decoded samples.
        assert .55 <= decoded_seconds <= .69


@pytest.mark.parametrize("extension", ["mp4", "webm", "mkv", "mov"])
def test_short_transport_video_cut_decodes_all_frames_and_audio(transport_client, tmp_path, extension):
    response = download(transport_client, extension=extension)
    verify_decoded_clip(response, tmp_path / f"clipped.{extension}")


def test_short_transport_cut_can_mute_audio(transport_client, tmp_path):
    response = download(transport_client, mute=True, normalize=False)
    verify_decoded_clip(response, tmp_path / "muted.mp4", audio=False)


@pytest.mark.parametrize("normalize", [False, True])
def test_short_transport_audio_cut_decodes_the_requested_duration(transport_client, tmp_path, normalize):
    response = download(transport_client, kind="audio", extension="mp3", normalize=normalize)
    verify_decoded_clip(response, tmp_path / "audio.mp3", video=False)


def test_short_cut_aligns_separate_transport_video_and_m4a_audio(transport_client, tmp_path):
    transport_client.source_mode["separate"] = True
    response = download(transport_client)
    verify_decoded_clip(response, tmp_path / "separate.mp4")
