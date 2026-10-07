import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from yt_dlp.networking import Response

from api import engine, index, player, security
from api.security import AudioError, Guard


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


@pytest.mark.parametrize("url,provider,host,path", [
    ("https://youtu.be/jNQXAC9IVRw?t=20", "YouTube", "www.youtube-nocookie.com", "/embed/jNQXAC9IVRw"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw&list=other", "YouTube", "www.youtube-nocookie.com", "/embed/jNQXAC9IVRw"),
    ("https://www.youtube.com/shorts/jNQXAC9IVRw", "YouTube", "www.youtube-nocookie.com", "/embed/jNQXAC9IVRw"),
    ("https://www.tiktok.com/@creator/video/7411992760518208800", "TikTok", "www.tiktok.com", "/player/v1/7411992760518208800"),
    ("https://vimeo.com/76979871", "Vimeo", "player.vimeo.com", "/video/76979871"),
    ("https://www.dailymotion.com/video/x84sh87", "Dailymotion", "www.dailymotion.com", "/embed/video/x84sh87"),
    ("https://dai.ly/x84sh87", "Dailymotion", "www.dailymotion.com", "/embed/video/x84sh87"),
    ("https://www.twitch.tv/videos/12345678", "Twitch", "player.twitch.tv", "/"),
    ("https://clips.twitch.tv/CreativeClipId", "Twitch", "clips.twitch.tv", "/embed"),
    ("https://www.facebook.com/example/videos/123456789", "Facebook", "www.facebook.com", "/plugins/video.php"),
    ("https://www.facebook.com/reel/123456789", "Facebook", "www.facebook.com", "/plugins/video.php"),
    ("https://www.instagram.com/reel/Cxyz_12345/", "Instagram", "www.instagram.com", "/reel/Cxyz_12345/embed/"),
    ("https://www.bilibili.com/video/BV1xx411c7mD/", "Bilibili", "player.bilibili.com", "/player.html"),
])
def test_official_player_urls_cannot_inject_arbitrary_frames(url, provider, host, path):
    descriptor = player.official_embed(url, "preview-123.vercel.app")
    assert descriptor["kind"] == "embed"
    assert descriptor["provider"] == provider
    parsed = player.urllib.parse.urlsplit(descriptor["url"])
    assert parsed.scheme == "https"
    assert parsed.hostname == host and parsed.hostname in player.FRAME_HOSTS
    assert parsed.path == path
    assert "autoplay=1" not in parsed.query


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.example/watch?v=jNQXAC9IVRw",
    "https://www.youtube.com/watch?v=jNQXAC9IVRw%22onload%3Dalert(1)",
    "https://youtu.be/jNQXAC9IVRw/other",
    "https://www.youtube.com/playlist?list=PL12345",
    "https://www.tiktok.com/@user/video/123%2F..%2Fother",
    "https://www.instagram.com/p/../../other",
    "https://vimeo.com/123456/../../other",
    "https://www.facebook.com/plugins/video.php?href=https://evil.example",
])
def test_unknown_or_invalid_ids_do_not_become_iframes(url):
    assert player.official_embed(url) is None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                      "http://127.0.0.1/video.mp4", "https://user:secret@youtube.com/watch?v=jNQXAC9IVRw"])
def test_source_preview_rejects_private_schemes_and_credentials(url):
    with pytest.raises(AudioError):
        player.resolve_player(url, "onda-audio.vercel.app", Guard())


def test_twitch_parent_is_encoded_and_invalid_host_is_replaced():
    descriptor = player.official_embed("https://www.twitch.tv/videos/12345678", 'evil"&parent=127.0.0.1')
    query = player.urllib.parse.parse_qs(player.urllib.parse.urlsplit(descriptor["url"]).query)
    assert query["parent"] == ["onda-audio.vercel.app"]
    assert query["autoplay"] == ["false"]


def test_vimeo_private_hash_preserved_but_not_arbitrary_query():
    descriptor = player.official_embed("https://vimeo.com/123456789/abc123def4?evil=1")
    query = player.urllib.parse.parse_qs(player.urllib.parse.urlsplit(descriptor["url"]).query)
    assert query == {"h": ["abc123def4"], "autoplay": ["0"], "dnt": ["1"]}


def format_item(**values):
    return {"url": "https://cdn.example.com/video.mp4", "protocol": "https", "ext": "mp4",
            "vcodec": "avc1.64001f", "acodec": "mp4a.40.2", "height": 720, **values}


def test_progressive_selection_requires_browser_codecs_and_mixed_video_audio():
    accepted = format_item()
    audio = format_item(ext="m4a", vcodec="none", height=None)
    rejected = [format_item(protocol="m3u8_native"), format_item(acodec="none"),
                format_item(vcodec="av01.0.05M.08"), format_item(has_drm=True),
                format_item(url="http://cdn.example.com/v.mp4"), format_item(height=2160),
                format_item(filesize=engine.MAX_SOURCE + 1)]
    candidates = player._candidate_formats({"formats": rejected + [audio, accepted]})
    assert candidates == [(accepted, "video"), (audio, "audio")]


def fake_transport(monkeypatch, *, mime="video/mp4", redirected="https://cdn.example.com/final.mp4", size=1024, fail_head=False):
    calls = []
    class Transport:
        def __init__(self, guard): self.guard = guard
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, request):
            calls.append(request)
            if fail_head and request.method == "HEAD":
                raise RuntimeError("HTTP Error 405")
            return Response(io.BytesIO(b"x"), redirected, {"Content-Type": mime, "Content-Length": str(size)})
    monkeypatch.setattr(engine, "direct_handler", Transport)
    return calls


def fake_extractor(monkeypatch, info):
    seen = []
    class Extractor:
        def __init__(self, opts, guard, cookiejar=None):
            seen.append({"options": opts, "cookies": cookiejar})
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def extract_info(self, url, download=False):
            assert download is False
            return info
    monkeypatch.setattr(engine, "SafeYoutubeDL", Extractor)
    monkeypatch.setattr(engine, "source_for", lambda _url: "Example platform")
    return seen


def test_direct_file_preview_verifies_public_response_and_redirect(monkeypatch):
    calls = fake_transport(monkeypatch)
    result = player.resolve_player("https://example.com/video.mp4", "onda-audio.vercel.app", Guard())
    assert result["kind"] == "direct" and result["media_type"] == "video"
    assert result["url"] == "https://cdn.example.com/final.mp4"
    assert calls[0].method == "HEAD"


def test_audio_only_direct_link_uses_an_audio_player(monkeypatch):
    fake_transport(monkeypatch, mime="audio/mpeg", redirected="https://cdn.example.com/final.mp3")
    result = player.resolve_player("https://example.com/song.mp3", "onda-audio.vercel.app", Guard())
    assert result["kind"] == "direct" and result["media_type"] == "audio"


def test_direct_preview_head_fallback_requests_only_first_byte(monkeypatch):
    calls = fake_transport(monkeypatch, fail_head=True)
    assert player.resolve_player("https://example.com/video.mp4", "onda-audio.vercel.app", Guard())["kind"] == "direct"
    assert len(calls) == 2 and calls[1].headers["Range"] == "bytes=0-0"


@pytest.mark.parametrize("redirected", ["https://127.0.0.1/private.mp4", "https://10.0.0.5/v.mp4", "https://user:password@cdn.example.com/file.mp4"])
def test_direct_preview_cannot_redirect_browser_to_private_or_credentialled_media(monkeypatch, redirected):
    fake_transport(monkeypatch, redirected=redirected)
    with pytest.raises(AudioError) as error:
        player.resolve_player("https://example.com/video.mp4", "onda-audio.vercel.app", Guard())
    assert error.value.code == "unsafe_url"


@pytest.mark.parametrize("mime,redirected", [("text/html", "https://example.com/v.mp4"),
                                           ("video/mp2t", "https://example.com/v.ts"),
                                           ("application/octet-stream", "https://example.com/v.mkv")])
def test_unsupported_direct_container_never_falsely_claims_playback(monkeypatch, mime, redirected):
    fake_transport(monkeypatch, mime=mime, redirected=redirected)
    result = player.resolve_player("https://example.com/v.mp4", "onda-audio.vercel.app", Guard())
    assert result["kind"] == "unavailable"


def test_extracted_progressive_media_is_checked_without_private_headers(monkeypatch):
    fake_extractor(monkeypatch, {"title": "My video", "formats": [format_item(http_headers={"Cookie": "SID=secret", "Authorization": "secret"})]})
    calls = fake_transport(monkeypatch)
    result = player.resolve_player("https://example.com/watch/123", "onda-audio.vercel.app", Guard())
    assert result["kind"] == "direct" and result["title"] == "My video"
    assert "Cookie" not in calls[0].headers and "Authorization" not in calls[0].headers
    assert "secret" not in str(result)


def test_adaptive_only_platform_gets_truthful_unavailable_player(monkeypatch):
    fake_extractor(monkeypatch, {"formats": [format_item(protocol="m3u8_native"), format_item(acodec="none")]})
    monkeypatch.setattr(engine, "direct_handler", lambda *_args: pytest.fail("Adaptive media should not be sent to browser"))
    assert player.resolve_player("https://example.com/watch/123", "onda-audio.vercel.app", Guard())["kind"] == "unavailable"


def test_official_embed_needs_no_server_extraction_or_dns(monkeypatch):
    monkeypatch.setattr(engine, "SafeYoutubeDL", lambda *_args, **_kwargs: pytest.fail("Official embed must not extract"))
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: pytest.fail("Safe fixed provider needs no origin DNS"))
    result = player.resolve_player("https://youtu.be/jNQXAC9IVRw", "onda-audio.vercel.app", Guard(), cookies="private-session")
    assert result["kind"] == "embed"
    assert "private-session" not in str(result)


@pytest.fixture
def player_client():
    app = FastAPI()
    app.add_exception_handler(AudioError, index.audio_error)
    player.register(app)
    with TestClient(app) as client:
        yield client


def test_api_official_embeds_do_not_take_download_capacity(player_client):
    index.SLOTS.acquire()
    index.SLOTS.acquire()
    try:
        response = player_client.post("/api/player", json={"url": "https://youtu.be/jNQXAC9IVRw"})
        assert response.status_code == 200 and response.json()["kind"] == "embed"
        busy = player_client.post("/api/player", json={"url": "https://example.com/v.mp4"})
        assert busy.status_code == 429 and busy.json()["code"] == "busy"
    finally:
        index.SLOTS.release()
        index.SLOTS.release()


def test_api_capacity_released_after_origin_error(player_client, monkeypatch):
    fake_transport(monkeypatch, mime="text/html")
    response = player_client.post("/api/player", json={"url": "https://example.com/v.mp4"})
    assert response.status_code == 200 and response.json()["kind"] == "unavailable"
    assert index.SLOTS.acquire(blocking=False)
    assert index.SLOTS.acquire(blocking=False)
    index.SLOTS.release()
    index.SLOTS.release()


def test_api_rejects_private_urls_and_unknown_body_fields(player_client):
    assert player_client.post("/api/player", json={"url": "https://127.0.0.1/v.mp4"}).status_code == 400
    assert player_client.post("/api/player", json={"url": "https://example.com/v.mp4", "headers": {"Cookie": "secret"}}).status_code == 422


def test_media_candidate_private_dns_is_rejected(monkeypatch):
    fake_extractor(monkeypatch, {"formats": [format_item()]})
    def dns(host, *_args, **_kwargs):
        address = "10.0.0.5" if host == "cdn.example.com" else "93.184.216.34"
        return [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", (address, 443))]
    monkeypatch.setattr(security.socket, "getaddrinfo", dns)
    with pytest.raises(AudioError) as error:
        player.resolve_player("https://example.com/watch/123", "onda-audio.vercel.app", Guard())
    assert error.value.code == "unsafe_url"
