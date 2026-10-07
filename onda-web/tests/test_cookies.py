import time
import urllib.request

import pytest

from api import cookies, engine, index, security
from api.security import AudioError, Guard

YOUTUBE = "https://www.youtube.com/watch?v=BaW_jenozKc"
HEADER = "# Netscape HTTP Cookie File\n"


def row(domain=".youtube.com", *, subdomains="TRUE", path="/", secure="TRUE", expiry="0", name="session", value="private-test-value"):
    return "\t".join((domain, subdomains, path, secure, expiry, name, value)) + "\n"


def cookie_header(jar, url):
    request = urllib.request.Request(url)
    jar.add_cookie_header(request)
    return request.get_header("Cookie")


def test_memory_jar_uses_original_domain_scope_and_never_saves(tmp_path):
    jar = cookies.parse_netscape(HEADER + row(), YOUTUBE)
    assert cookie_header(jar, "https://www.youtube.com/watch?v=BaW_jenozKc") == "session=private-test-value"
    assert cookie_header(jar, "https://evil.example.com/file.mp3") is None
    assert cookie_header(jar, "https://soundcloud.com/artist/song") is None
    assert cookie_header(jar, "https://www.google.com/") is None
    assert cookie_header(jar, "http://www.youtube.com/") is None
    assert jar.filename is None
    with pytest.raises(RuntimeError):
        jar.save(tmp_path / "cookies.txt")
    assert not (tmp_path / "cookies.txt").exists()


def test_ignores_foreign_sites_from_browser_export_and_honors_http_only():
    jar = cookies.parse_netscape(HEADER + "#HttpOnly_" + row() + row(".facebook.com", value="other-secret"), YOUTUBE)
    assert len(jar) == 1
    assert next(iter(jar)).has_nonstandard_attr("HttpOnly")
    assert cookie_header(jar, "https://www.facebook.com/") is None


def test_google_cookie_stays_google_and_cannot_be_cloned_onto_youtube():
    jar = cookies.parse_netscape(HEADER + row(".google.com"), YOUTUBE)
    assert cookie_header(jar, "https://accounts.google.com/") == "session=private-test-value"
    assert cookie_header(jar, "https://www.youtube.com/") is None
    assert cookie_header(jar, "https://www.googlevideo.com/") is None


def test_host_only_cookies_are_not_sent_to_sibling_or_subdomain():
    jar = cookies.parse_netscape(HEADER + row("www.youtube.com", subdomains="FALSE"), YOUTUBE)
    assert cookie_header(jar, "https://www.youtube.com/")
    assert cookie_header(jar, "https://music.youtube.com/") is None
    assert cookie_header(jar, "https://sub.www.youtube.com/") is None


@pytest.mark.parametrize("domain", [".com", ".co.uk", ".github.io", ".appspot.com", "..youtube.com", ".localhost", ".127.0.0.1"])
def test_rejects_public_private_suffix_or_nonpublic_domains(domain):
    with pytest.raises(AudioError) as raised:
        cookies.parse_netscape(HEADER + row(domain), YOUTUBE)
    assert raised.value.code == "cookie_domain"
    assert "private-test-value" not in raised.value.message


@pytest.mark.parametrize("data", [
    "session=secret", HEADER + "one\ttwo\n", HEADER + row(secure="maybe"),
    HEADER + row(subdomains="YES"), HEADER + row(expiry="never"), HEADER + row(path="relative"),
    HEADER + row(value="cookie; Authorization=secret"), HEADER + row(value="injected\x00data"),
    HEADER + row(name="invalid cookie"), HEADER + row(".youtube.com", subdomains="FALSE"),
])
def test_rejects_malformed_netscape_without_echoing_contents(data):
    with pytest.raises(AudioError) as raised:
        cookies.parse_netscape(data, YOUTUBE)
    assert raised.value.code in {"invalid_cookies", "cookie_domain"}
    assert "Authorization" not in raised.value.message


def test_cookie_limits_bytes_rows_and_expired_values():
    with pytest.raises(AudioError):
        cookies.parse_netscape(HEADER + "é" * (cookies.MAX_COOKIE_BYTES // 2), YOUTUBE)
    with pytest.raises(AudioError):
        cookies.parse_netscape(HEADER + row() * (cookies.MAX_COOKIES + 1), YOUTUBE)
    with pytest.raises(AudioError) as raised:
        cookies.parse_netscape(HEADER + row(expiry=str(int(time.time()) - 1)), YOUTUBE)
    assert raised.value.code == "cookies_expired"


def test_other_services_use_psl_registrable_domain_and_private_tenant_boundary():
    jar = cookies.parse_netscape(HEADER + row(".bbc.co.uk"), "https://www.bbc.co.uk/programmes/p0jxy1dg")
    assert cookie_header(jar, "https://www.bbc.co.uk/")
    assert cookie_header(jar, "https://unrelated.co.uk/") is None
    jar = cookies.parse_netscape(HEADER + row("foo.github.io"), "https://foo.github.io/video")
    assert cookie_header(jar, "https://foo.github.io/video")
    assert cookie_header(jar, "https://bar.github.io/video") is None


def test_rejects_cookies_on_direct_files_and_insecure_platform_links():
    with pytest.raises(AudioError) as raised:
        cookies.parse_netscape(HEADER + row(), "https://example.com/audio.mp3", direct_media=True)
    assert raised.value.code == "cookies_not_supported"
    with pytest.raises(AudioError) as raised:
        cookies.parse_netscape(HEADER + row(), YOUTUBE.replace("https:", "http:"))
    assert raised.value.code == "cookie_https_required"


def test_request_model_hides_cookie_values_and_enforces_utf8_byte_limit():
    model = index.LinkInput(url=YOUTUBE, cookies=HEADER + row())
    assert "private-test-value" not in repr(model)
    with pytest.raises(AudioError):
        index.LinkInput(url=YOUTUBE, cookies="é" * (cookies.MAX_COOKIE_BYTES // 2 + 1))


def test_instance_session_is_cleared_on_success_and_failure(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    observed_jars = []
    def fake_extract(downloader, _url, download=False):
        observed_jars.append(downloader.cookiejar)
        assert cookie_header(downloader.cookiejar, YOUTUBE) == "session=private-test-value"
        return {"title": "Teste", "duration": 1}
    monkeypatch.setattr(engine.SafeYoutubeDL, "extract_info", fake_extract)
    result = engine.inspect_media(YOUTUBE, Guard(), cookies=HEADER + row())
    assert result["title"] == "Teste"
    assert len(observed_jars[0]) == 0
    def fake_failure(downloader, _url, download=False):
        observed_jars.append(downloader.cookiejar)
        raise RuntimeError("private-test-value provider error")
    monkeypatch.setattr(engine.SafeYoutubeDL, "extract_info", fake_failure)
    with pytest.raises(AudioError) as raised:
        engine.inspect_media(YOUTUBE, Guard(), cookies=HEADER + row())
    assert len(observed_jars[-1]) == 0
    assert "private-test-value" not in raised.value.message


@pytest.mark.parametrize("url", [
    "https://www.ted.com/talks/ken_robinson_do_schools_kill_creativity",
    "https://www.pinterest.com/pin/123456789012345678/",
    "https://www.bbc.co.uk/programmes/p0jxy1dg",
    "https://vk.com/video-1_123",
])
def test_supports_catalog_platforms_outside_old_static_list(url):
    assert engine.source_for(url) != "Arquivo direto"


def test_unknown_generic_webpage_remains_unsupported():
    with pytest.raises(AudioError) as raised:
        engine.source_for("https://example.com/article")
    assert raised.value.code == "unsupported_source"


def test_explicit_platform_extractor_precedes_direct_file_extension():
    assert engine.source_for("https://artist.bandcamp.com/track/song.mp3") == "Bandcamp"


def test_imported_session_cannot_bypass_policy_via_literal_cookie_header():
    from yt_dlp.networking import Request
    jar = cookies.parse_netscape(HEADER + row(), YOUTUBE)
    with engine.SafeYoutubeDL(engine.options(Guard()), Guard(), cookiejar=jar) as downloader:
        assert set(downloader._request_director.handlers) == {"ScopedPublic"}
        handler = next(iter(downloader._request_director.handlers.values()))
        headers = handler._get_headers(Request("https://evil.example.com/audio", headers={"Cookie": "session=private-test-value"}))
        assert "Cookie" not in headers
        assert cookie_header(jar, "https://evil.example.com/audio") is None


def test_guarded_redirect_cannot_forward_imported_session_to_other_service(monkeypatch):
    import io
    from email.message import Message
    from urllib.response import addinfourl
    from yt_dlp.networking import Request
    requests = []
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])

    class FakeHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
        def __init__(self, *_args):
            urllib.request.HTTPHandler.__init__(self)
        def https_open(self, request):
            requests.append((request.full_url, request.get_header("Cookie")))
            headers = Message()
            if len(requests) == 1:
                headers["Location"] = "https://soundcloud.com/artist/song"
                code = 302
            else:
                code = 200
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, code)
            response.msg = "Found" if code == 302 else "OK"
            return response
        http_open = https_open

    monkeypatch.setattr(security, "PublicHTTPHandler", FakeHTTPHandler)
    jar = cookies.parse_netscape(HEADER + row(), YOUTUBE)
    guard = Guard()
    with engine.SafeYoutubeDL(engine.options(guard), guard, cookiejar=jar) as downloader:
        with downloader.urlopen(Request(YOUTUBE, headers={"Cookie": "forged=header-value"})) as response:
            assert response.url == "https://soundcloud.com/artist/song"
    assert requests[0][1] == "session=private-test-value"
    assert requests[1][1] is None
