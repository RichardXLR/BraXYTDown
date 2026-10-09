import json
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


def json_cookie(**changes):
    item = {"domain": ".youtube.com", "hostOnly": False, "httpOnly": True,
            "path": "/", "secure": True, "session": True,
            "name": "session", "value": "private-test-value"}
    item.update(changes)
    return item


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


@pytest.mark.parametrize("wrapped", [False, True])
def test_browser_json_exports_keep_cookie_scope_and_accept_legacy_parser_entrypoint(wrapped):
    entries = [json_cookie(), json_cookie(domain=".facebook.com", value="other-secret")]
    payload = {"cookies": entries, "exportedAt": "2026-10-08"} if wrapped else entries
    jar = cookies.parse_netscape("\ufeff  " + json.dumps(payload), YOUTUBE)
    assert len(jar) == 1
    assert cookie_header(jar, YOUTUBE) == "session=private-test-value"
    assert cookie_header(jar, "http://www.youtube.com/") is None
    assert cookie_header(jar, "https://soundcloud.com/") is None
    assert cookie_header(jar, "https://www.facebook.com/") is None
    assert next(iter(jar)).has_nonstandard_attr("HttpOnly")
    assert jar.filename is None


def test_browser_json_host_only_and_google_cookies_do_not_move_between_hosts():
    jar = cookies.parse_session(json.dumps([
        json_cookie(domain="www.youtube.com", hostOnly=True, name="host_session"),
        json_cookie(domain=".google.com", name="google_session", value="google-private-test"),
    ]), YOUTUBE)
    assert cookie_header(jar, "https://www.youtube.com/") == "host_session=private-test-value"
    assert cookie_header(jar, "https://music.youtube.com/") is None
    assert cookie_header(jar, "https://sub.www.youtube.com/") is None
    assert cookie_header(jar, "https://accounts.google.com/") == "google_session=google-private-test"
    assert cookie_header(jar, "https://www.googlevideo.com/") is None


def test_browser_json_defaults_never_widen_a_host_only_cookie():
    jar = cookies.parse_session(json.dumps([
        {"domain": "www.youtube.com", "name": "session", "value": "private-test-value"},
    ]), YOUTUBE)
    assert cookie_header(jar, "https://www.youtube.com/") == "session=private-test-value"
    assert cookie_header(jar, "https://sub.www.youtube.com/") is None
    assert cookie_header(jar, "https://music.youtube.com/") is None
    assert cookie_header(jar, "http://www.youtube.com/") is None


def test_json_validation_returns_only_safe_metadata_and_counts_matching_expired_cookies(monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(cookies.time, "time", lambda: now)
    data = json.dumps([
        json_cookie(name="hidden-name", value="hidden-secret"),
        json_cookie(domain=".google.com", name="other-hidden-name", value="other-hidden-secret",
                    session=False, expirationDate=now + 90.75),
        json_cookie(name="expired-secret-name", session=False, expirationDate=now - 1),
        json_cookie(domain=".facebook.com", value="unrelated-secret"),
        json_cookie(name="partitioned-secret-name", partitionKey={"topLevelSite": "https://youtube.com"}),
    ])
    summary = cookies.validate_session(data, YOUTUBE)
    assert summary == {"format": "json", "validCookies": 2, "ignoredCookies": 2, "expiredCookies": 1,
                       "scopes": ["google.com", "youtube.com"], "earliestExpiry": now + 90,
                       "sessionCookies": 1}
    assert "secret" not in repr(summary)
    assert "hidden-name" not in repr(summary)


def test_netscape_validation_uses_the_same_safe_metadata(monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(cookies.time, "time", lambda: now)
    data = (HEADER + row() + row(".google.com", expiry=str(now + 30)) + row(".facebook.com")
            + row(name="expired-session", expiry=str(now - 1)))
    assert cookies.validate_session(data, YOUTUBE) == {
        "format": "netscape", "validCookies": 2, "ignoredCookies": 1, "expiredCookies": 1,
        "scopes": ["google.com", "youtube.com"], "earliestExpiry": now + 30, "sessionCookies": 1,
    }


@pytest.mark.parametrize("expiry_field", ["expires", "expirationDate"])
def test_json_fractional_expiry_is_finite_and_session_sentinels_are_preserved(monkeypatch, expiry_field):
    now = 2_000_000_000
    monkeypatch.setattr(cookies.time, "time", lambda: now)
    persistent = json_cookie(session=False, **{expiry_field: now + 120.875})
    jar = cookies.parse_session(json.dumps([persistent]), YOUTUBE)
    cookie = next(iter(jar))
    assert cookie.expires == now + 120
    assert not cookie.discard
    session = json_cookie(**{expiry_field: -1})
    session.pop("session")
    cookie = next(iter(cookies.parse_session(json.dumps([session]), YOUTUBE)))
    assert cookie.expires is None and cookie.discard


@pytest.mark.parametrize("changes", [
    {"expirationDate": float("nan")}, {"expirationDate": float("inf")},
    {"expirationDate": float("-inf")}, {"expirationDate": True}, {"expirationDate": "2100000000"},
    {"expirationDate": 10 ** 1000}, {"expires": -0.5}, {"expires": -2},
    {"expirationDate": 2_100_000_000, "expires": 2_100_000_001},
    {"hostOnly": "false"}, {"hostOnly": None}, {"secure": None}, {"httpOnly": 1},
    {"session": None}, {"partitioned": "false"}, {"path": "relative"}, {"path": None},
    {"value": None}, {"name": "bad name"}, {"value": "cookie; Authorization=secret"},
    {"value": "injected\r\nAuthorization: secret"}, {"value": "injected\x00data"},
    {"value": "injected\ud800data"}, {"session": False},
])
def test_json_rejects_malformed_or_injectable_values_without_echoing_content(changes):
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([json_cookie(**changes)]), YOUTUBE)
    assert raised.value.code == "invalid_cookies"
    assert "Authorization" not in raised.value.message
    assert "private-test-value" not in raised.value.message


@pytest.mark.parametrize("domain", [".com", ".co.uk", ".github.io", "..youtube.com", ".localhost", ".127.0.0.1"])
def test_json_rejects_unsafe_cookie_domains(domain):
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([json_cookie(domain=domain)]), YOUTUBE)
    assert raised.value.code == "cookie_domain"


@pytest.mark.parametrize("data", [
    "[{]", "{}", '{"cookies": {}}', '[null]', '{"cookies": [true]}',
    '[{"domain":".youtube.com","name":"session","value":"first","value":"second"}]',
    '[{"domain":".youtube.com","name":"session","value":"secret"}] trailing',
])
def test_rejects_ambiguous_or_malformed_json_exports(data):
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(data, YOUTUBE)
    assert raised.value.code == "invalid_cookies"
    assert "secret" not in raised.value.message


def test_json_export_limits_count_every_row_even_foreign_or_duplicate_entries():
    duplicate_data = json.dumps([json_cookie()] * (cookies.MAX_COOKIES + 1))
    assert len(duplicate_data.encode()) < cookies.MAX_COOKIE_BYTES
    with pytest.raises(AudioError):
        cookies.parse_session(duplicate_data, YOUTUBE)
    foreign_data = json.dumps([json_cookie(domain=".facebook.com")] * cookies.MAX_COOKIES + [json_cookie()])
    with pytest.raises(AudioError):
        cookies.parse_session(foreign_data, YOUTUBE)
    large_data = json.dumps([json_cookie(value="é" * 2500)] * 20, ensure_ascii=False)
    assert len(large_data.encode("utf-8")) > cookies.MAX_COOKIE_BYTES
    with pytest.raises(AudioError):
        cookies.parse_session(large_data, YOUTUBE)


def test_json_expired_only_and_foreign_expired_entries_are_distinguished(monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(cookies.time, "time", lambda: now)
    with pytest.raises(AudioError) as raised:
        cookies.validate_session(json.dumps([json_cookie(session=False, expires=now - 1)]), YOUTUBE)
    assert raised.value.code == "cookies_expired"
    with pytest.raises(AudioError) as raised:
        cookies.validate_session(json.dumps([json_cookie(domain=".facebook.com", session=False, expires=now - 1)]), YOUTUBE)
    assert raised.value.code == "cookie_domain"


def test_json_expiry_cannot_be_revived_by_a_stale_session_flag_or_duplicate(monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(cookies.time, "time", lambda: now)
    expired = json_cookie(session=True, expirationDate=now - 1)
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([expired]), YOUTUBE)
    assert raised.value.code == "cookies_expired"
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([json_cookie(), expired]), YOUTUBE)
    assert raised.value.code == "cookies_expired"
    # Export tools using 0 or -1 for an actual session cookie remain accepted.
    assert cookies.validate_session(json.dumps([json_cookie(expirationDate=0)]), YOUTUBE)["sessionCookies"] == 1
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(HEADER + row() + row(expiry=str(now - 1)), YOUTUBE)
    assert raised.value.code == "cookies_expired"


@pytest.mark.parametrize("expiry_field", ["expires", "expirationDate"])
@pytest.mark.parametrize("expiry, session, error", [
    (0.5, True, "cookies_expired"), (0.5, False, "cookies_expired"),
    (0.000_000_001, True, "cookies_expired"), (-0.000_000_001, True, "invalid_cookies"),
    (-1, True, None), (0, True, None), (0.0, True, None),
])
def test_json_session_zero_sentinel_uses_original_expiry_before_rounding(expiry_field, expiry, session, error):
    data = json.dumps([json_cookie(session=session, **{expiry_field: expiry})])
    if error:
        with pytest.raises(AudioError) as raised:
            cookies.validate_session(data, YOUTUBE)
        assert raised.value.code == error
    else:
        summary = cookies.validate_session(data, YOUTUBE)
        assert summary["validCookies"] == summary["sessionCookies"] == 1
        assert summary["earliestExpiry"] is None


def test_json_mixed_expiry_fields_cannot_turn_a_positive_fraction_into_a_session():
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([json_cookie(expires=0, expirationDate=0.5)]), YOUTUBE)
    assert raised.value.code == "cookies_expired"


def test_json_partitioned_sessions_are_not_sent_without_partition_context():
    jar = cookies.parse_session(json.dumps([
        json_cookie(name="regular"), json_cookie(name="partitioned", partitioned=True),
    ]), YOUTUBE)
    assert cookie_header(jar, YOUTUBE) == "regular=private-test-value"
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([json_cookie(partitionKey={"topLevelSite": "https://youtube.com"})]), YOUTUBE)
    assert raised.value.code == "cookie_domain"


def test_firefox_first_party_isolation_is_not_removed_during_json_import():
    data = json.dumps([
        json_cookie(name="regular", firstPartyDomain=""),
        json_cookie(name="isolated", value="isolated-private-value", firstPartyDomain="youtube.com"),
    ])
    jar = cookies.parse_session(data, YOUTUBE)
    assert cookie_header(jar, YOUTUBE) == "regular=private-test-value"
    summary = cookies.validate_session(data, YOUTUBE)
    assert summary["validCookies"] == 1 and summary["ignoredCookies"] == 1
    assert "isolated-private-value" not in repr(summary)
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(json.dumps([json_cookie(firstPartyDomain="youtube.com")]), YOUTUBE)
    assert raised.value.code == "cookie_domain"


def test_json_matching_duplicates_use_last_value_and_metadata_counts_actual_jar():
    jar = cookies.parse_session(json.dumps([json_cookie(value="old-private-value"), json_cookie(value="new-private-value")]), YOUTUBE)
    assert len(jar) == 1
    assert cookie_header(jar, YOUTUBE) == "session=new-private-value"
    assert cookies.validate_session(json.dumps([json_cookie(), json_cookie()]), YOUTUBE)["validCookies"] == 1


def test_validation_clears_successful_sessions_and_failed_partial_imports(monkeypatch):
    jars = []
    original = cookies.RequestCookieJar

    class TrackedJar(original):
        def __init__(self, scopes):
            super().__init__(scopes)
            jars.append(self)

    monkeypatch.setattr(cookies, "RequestCookieJar", TrackedJar)
    assert cookies.validate_session(json.dumps([json_cookie()]), YOUTUBE)["validCookies"] == 1
    assert len(jars[-1]) == 0
    with pytest.raises(AudioError):
        cookies.parse_session(json.dumps([json_cookie(), json_cookie(value="bad;cookie")]), YOUTUBE)
    assert len(jars[-1]) == 0


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_validation_requires_a_nonempty_export(raw):
    with pytest.raises(AudioError) as raised:
        cookies.validate_session(raw, YOUTUBE)
    assert raised.value.code == "invalid_cookies"


def test_json_sessions_keep_https_only_and_direct_file_restrictions():
    data = json.dumps([json_cookie()])
    with pytest.raises(AudioError) as raised:
        cookies.parse_session(data, YOUTUBE.replace("https:", "http:"))
    assert raised.value.code == "cookie_https_required"
    with pytest.raises(AudioError) as raised:
        cookies.validate_session(data, "https://example.com/video.mp4", direct_media=True)
    assert raised.value.code == "cookies_not_supported"
