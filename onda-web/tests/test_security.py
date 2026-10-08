import io
import http.client
from pathlib import Path
from unittest.mock import Mock

import pytest
from yt_dlp.networking import Request, Response
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.exceptions import HTTPError, IncompleteRead, TransportError

from api.engine import SafeYoutubeDL, options, source_for
from api.security import AudioError, Guard, PublicRH, PublicRedirectHandler, BoundedResponse, pinned_connection, public_url
import api.security as security


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/a.mp3", "https://user:pass@example.com/a",
    "http://127.0.0.1/a", "http://[::1]/a", "http://169.254.169.254/a",
    "http://10.0.0.1/a", "https://localhost/a", "https://router.local/a",
    "https://example.com:8080/a", "https://example.com:80/a",
    "https://example.com\\@127.0.0.1/a", "https://example.com/\na",
    "http://2130706433/a", "http://[::ffff:127.0.0.1]/a",
])
def test_rejects_unsafe_authorities_without_network(url):
    with pytest.raises(AudioError):
        public_url(url, resolve=False)


def dns_answer(ip):
    return (security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", (ip, 443))


def test_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [dns_answer("93.184.216.34"), dns_answer("192.168.1.1")])
    with pytest.raises(AudioError, match="público"):
        public_url("https://example.com/audio.mp3")


def test_redirect_revalidates_dns(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: [dns_answer("10.0.0.1")])
    guard = Guard()
    redirect = PublicRedirectHandler(guard)
    request = security.urllib.request.Request("https://example.com/audio.mp3")
    with pytest.raises(AudioError):
        redirect.redirect_request(request, None, 302, "Found", {}, "https://rebound.example.com/audio.mp3")
    assert guard.error.code == "unsafe_url"


def test_connection_uses_validated_ip_not_another_dns_lookup(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: [dns_answer("93.184.216.34")])
    socket_factory = Mock(return_value=Mock())
    monkeypatch.setattr(security.socket, "create_connection", socket_factory)
    connection = pinned_connection(security.http.client.HTTPConnection, Guard())("example.com", timeout=8)
    connection.connect()
    assert socket_factory.call_args.args[0] == ("93.184.216.34", 80)
    assert connection.host == "example.com"


def test_error_response_cannot_bypass_byte_budget(monkeypatch):
    def failed_request(_self, _request):
        raise HTTPError(Response(io.BytesIO(b"x" * 2048), "https://example.com/a", {}, 404))
    monkeypatch.setattr(UrllibRH, "_send", failed_request)
    guard = Guard(maximum_bytes=1024)
    handler = PublicRH(guard=guard, logger=Mock())
    with pytest.raises(HTTPError) as raised:
        handler.send(Request("https://example.com/a"))
    with pytest.raises(AudioError) as budget_error:
        raised.value.response.read(2048)
    assert budget_error.value.code == "source_too_large"


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b"partial", 30), IncompleteRead(7, 30)])
@pytest.mark.parametrize("budget,expected", [(20, 7), (5, 7)])
def test_partial_bytes_in_a_failed_read_still_consume_the_shared_budget(error, budget, expected):
    class Interrupted(io.BytesIO):
        def read(self, _amount=-1):
            raise error
    guard = Guard(maximum_bytes=budget)
    response = BoundedResponse(Response(Interrupted(b""), "https://cdn.example.com/media.mp4", {}), guard)
    with pytest.raises(AudioError if budget < expected else TransportError) as failed:
        response.read(64)
    assert guard.received == expected
    if budget < expected:
        assert failed.value.code == "source_too_large"


def test_only_guarded_handler_and_no_external_ffmpeg():
    guard = Guard()
    with SafeYoutubeDL(options(guard), guard) as downloader:
        assert set(downloader._request_director.handlers) == {"Public"}
        assert not Path(downloader.params["ffmpeg_location"]).exists()
        assert "no-external-ffmpeg" in downloader.params["ffmpeg_location"]


def test_generic_webpages_are_not_sent_to_extractor():
    with pytest.raises(AudioError) as raised:
        source_for("https://example.com/article")
    assert raised.value.code == "unsupported_source"
    assert source_for("https://www.youtube.com/watch?v=abc") == "YouTube"
    assert source_for("https://sample.example.com/demo.mp3") == "Arquivo direto"


def test_unsupported_hls_fails_before_external_downloader(monkeypatch):
    from api import engine
    monkeypatch.setattr(engine, "get_suitable_downloader", lambda *_args, **_kwargs: engine.HlsFD)
    guard = Guard()
    with SafeYoutubeDL(options(guard), guard) as downloader:
        with pytest.raises(AudioError) as raised:
            downloader.dl("unused.m4a", {"url": "https://example.com/audio.m3u8", "ext": "m4a",
                "hls_media_playlist_data": "#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"http://127.0.0.1/key\"\n#EXTINF:5\nhttp://127.0.0.1/segment.ts\n#EXT-X-ENDLIST\n"})
    assert raised.value.code == "unsupported_transfer"


def test_deadline_abort_closes_blocking_network_socket():
    guard = Guard()
    connection = Mock()
    guard.register_socket(connection)
    guard.abort()
    connection.shutdown.assert_called_once_with(security.socket.SHUT_RDWR)
    connection.close.assert_called_once()
    with pytest.raises(AudioError) as raised:
        guard.check()
    assert raised.value.code == "cancelled"
