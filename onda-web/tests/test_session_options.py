"""Request-only access options must preserve authentication and secret isolation."""
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.networking import Response

from api import auth, engine, index, player, recovery, security
from api.security import AudioError, Guard


VIDEO_URL = "https://vimeo.com/76979871"
PASSWORD = "fixture-video-password"
USER_AGENT = "Mozilla/5.0 (Fixture browser) Onda-Session-Test/1.0"
COOKIE_VALUE = "fixture-private-cookie"
COOKIE_EXPORT = json.dumps([{"domain": ".vimeo.com", "path": "/", "name": "session_test",
                             "value": COOKIE_VALUE, "secure": True, "session": True}])


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


@pytest.mark.parametrize("field,value,code", [
    ("video_password", "secret\r\nHeader: value", "invalid_video_password"),
    ("video_password", "secret\x00value", "invalid_video_password"),
    ("video_password", "secret\x85value", "invalid_video_password"),
    ("user_agent", "Browser\r\nCookie: secret", "invalid_user_agent"),
    ("user_agent", "Browser\x00", "invalid_user_agent"),
    ("user_agent", "Browser\tvalue", "invalid_user_agent"),
    ("user_agent", "Browser\x7f", "invalid_user_agent"),
    ("user_agent", "Browser ☃", "invalid_user_agent"),
])
@pytest.mark.parametrize("route", ["/api/inspect", "/api/download", "/api/compatibility/test", "/api/player"])
def test_session_control_characters_are_rejected_before_extraction(route, field, value, code, monkeypatch):
    monkeypatch.setattr(engine, "SafeYoutubeDL", lambda *_args, **_kwargs: pytest.fail("Invalid input reached extraction"))
    with TestClient(index.app) as client:
        response = client.post(route, json={"url": VIDEO_URL, field: value})
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert "secret" not in response.text and "Cookie:" not in response.text


@pytest.mark.parametrize("model", [index.LinkInput, index.DownloadInput, player.PlayerInput])
def test_secret_fields_have_bounds_and_never_appear_in_model_repr(model):
    body = model(url=VIDEO_URL, cookies=COOKIE_EXPORT, video_password=PASSWORD, user_agent=USER_AGENT)
    assert PASSWORD not in repr(body) and COOKIE_VALUE not in repr(body) and USER_AGENT not in repr(body)
    assert PASSWORD not in repr(body.session_options()) and USER_AGENT not in repr(body.session_options())
    for field, value in (("video_password", "x" * 257), ("user_agent", "x" * 513)):
        with pytest.raises(ValueError):
            model(url=VIDEO_URL, **{field: value})
    assert model(url=VIDEO_URL, video_password="", user_agent=" ").session_options() is None
    assert "video_password" not in engine.MediaSettings.__dataclass_fields__
    assert "user_agent" not in engine.MediaSettings.__dataclass_fields__


def test_cookie_validation_reports_only_metadata_without_claiming_platform_access():
    with TestClient(index.app) as client:
        response = client.post("/api/session/validate", json={"url": VIDEO_URL, "cookies": COOKIE_EXPORT})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"format": "json", "validCookies": 1, "ignoredCookies": 0,
                               "expiredCookies": 0, "scopes": ["vimeo.com"],
                               "earliestExpiry": None, "sessionCookies": 1}
    assert COOKIE_VALUE not in response.text and "session_test" not in response.text
    assert "accessGranted" not in response.json()


@pytest.mark.parametrize("payload,code", [
    ({"url": VIDEO_URL, "cookies": COOKIE_EXPORT, "video_password": PASSWORD}, "invalid_request"),
    ({"url": VIDEO_URL, "cookies": COOKIE_EXPORT, "user_agent": USER_AGENT}, "invalid_request"),
    ({"url": "https://cdn.example.com/video.mp4", "cookies": COOKIE_EXPORT}, "cookies_not_supported"),
    ({"url": "http://vimeo.com/76979871", "cookies": COOKIE_EXPORT}, "cookie_https_required"),
])
def test_cookie_validation_accepts_only_its_defined_request_and_platform(payload, code):
    with TestClient(index.app) as client:
        response = client.post("/api/session/validate", json=payload)
    assert response.status_code == 422 and response.json()["code"] == code
    assert COOKIE_VALUE not in response.text and PASSWORD not in response.text


def test_cookie_validation_requires_real_user_authentication():
    with TestClient(index.app) as client:
        response = client.post("/api/session/validate", json={"url": VIDEO_URL, "cookies": COOKIE_EXPORT},
                               headers={"Authorization": ""})
    assert response.status_code == 401 and response.json()["code"] == "auth_required"


@pytest.mark.parametrize("field,value", [("video_password", PASSWORD), ("user_agent", USER_AGENT),
                                          ("video_password", {"private": "value"}),
                                          ("user_agent", ["private"]), ("cookies", COOKIE_EXPORT)])
def test_autocura_machine_cannot_use_user_session_options(field, value, monkeypatch):
    async def verified_machine(_token, _configuration):
        return auth.AuthPrincipal("machine", machine_id="mch_fixture")
    monkeypatch.setattr(auth, "_verify_machine", verified_machine)
    with TestClient(index.app) as client:
        response = client.post("/api/download", json={"url": "https://onda-audio.vercel.app/canary.wav", field: value},
                               headers={"Authorization": "Bearer mt_fixture"})
        validation = client.post("/api/session/validate", json={"url": VIDEO_URL, "cookies": COOKIE_EXPORT},
                                 headers={"Authorization": "Bearer mt_fixture"})
    assert response.status_code == validation.status_code == 403
    assert PASSWORD not in response.text and COOKIE_VALUE not in response.text


@pytest.mark.parametrize("route", ["/api/inspect", "/api/compatibility/test"])
def test_metadata_routes_pass_request_options_without_returning_them(route, monkeypatch):
    observed = []
    def inspect(url, guard, **kwargs):
        observed.append(kwargs)
        return {"title": "Allowed video", "source": "Vimeo"}
    monkeypatch.setattr(index, "inspect_media", inspect)
    monkeypatch.setattr(engine, "inspect_media", inspect)
    with TestClient(index.app) as client:
        response = client.post(route, json={"url": VIDEO_URL, "video_password": PASSWORD, "user_agent": USER_AGENT})
    assert response.status_code == 200, response.text
    assert observed[0]["session"] == engine.SessionOptions(PASSWORD, USER_AGENT)
    assert PASSWORD not in response.text and USER_AGENT not in response.text


def test_download_route_keeps_options_separate_from_saved_media_settings(monkeypatch):
    observed = []
    def prepare(url, directory, fmt, quality, guard, **kwargs):
        observed.append(kwargs)
        target = directory / "download.mp3"
        target.write_bytes(b"test-media")
        return target, {"title": "Allowed video"}
    monkeypatch.setattr(index, "prepare_download", prepare)
    with TestClient(index.app) as client:
        response = client.post("/api/download", json={"url": VIDEO_URL, "video_password": PASSWORD, "user_agent": USER_AGENT})
    assert response.status_code == 200 and response.content == b"test-media"
    assert observed[0]["session"] == engine.SessionOptions(PASSWORD, USER_AGENT)
    assert observed[0]["settings"] == engine.MediaSettings()
    assert PASSWORD not in str(response.headers) and USER_AGENT not in str(response.headers)


def test_session_options_reach_yt_dlp_metadata_but_never_the_response(monkeypatch):
    observed = []
    class Extractor:
        def __init__(self, opts, guard, cookiejar=None): observed.append(opts)
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def extract_info(self, _url, download=False): return {"title": "Allowed video"}
    monkeypatch.setattr(engine, "SafeYoutubeDL", Extractor)
    result = engine.inspect_media(VIDEO_URL, Guard(), session=engine.SessionOptions(PASSWORD, USER_AGENT))
    assert observed[0]["videopassword"] == PASSWORD
    assert observed[0]["http_headers"]["User-Agent"] == USER_AGENT
    assert PASSWORD not in str(result) and USER_AGENT not in str(result)


@pytest.mark.parametrize("media_type", ["audio", "video"])
def test_recovery_keeps_session_options_for_every_attempt_and_parallel_track(media_type, tmp_path, monkeypatch):
    observed, extractions = [], []
    class Extractor:
        def __init__(self, opts, guard, cookiejar=None):
            self.opts, self.cookiejar = opts, cookiejar or YoutubeDLCookieJar()
            observed.append(opts)
        def __enter__(self): return self
        def __exit__(self, *_args): self.cookiejar.clear()
        def extract_info(self, _url, download=False):
            extractions.append(download)
            if len(extractions) == 1:
                raise RuntimeError("HTTP Error 502")
            return {"title": "Allowed video", "duration": 2, "formats": [
                {"format_id": "v", "url": "https://cdn.example.com/v.mp4", "protocol": "https", "ext": "mp4",
                 "vcodec": "avc1", "acodec": "none", "width": 1280, "height": 720},
                {"format_id": "a", "url": "https://cdn.example.com/a.m4a", "protocol": "https", "ext": "m4a",
                 "vcodec": "none", "acodec": "aac", "abr": 128},
            ]}
        def process_ie_result(self, _info, download=False):
            assert download
            Path(self.opts["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(b"audio")
        def _calc_headers(self, _info): return self.opts["http_headers"]
        def dl(self, path, info):
            assert info["http_headers"]["User-Agent"] == USER_AGENT
            Path(path).write_bytes(b"allowed-track")
            return True, None
    monkeypatch.setattr(engine, "SafeYoutubeDL", Extractor)
    monkeypatch.setattr(engine, "wait_for_retry", lambda *_args: True)
    result, details = engine.acquire_with_recovery(VIDEO_URL, tmp_path, "mp4" if media_type == "video" else "mp3",
                                                 Guard(), engine.MediaSettings(media_type=media_type), None,
                                                 session=engine.SessionOptions(PASSWORD, USER_AGENT))
    assert len(extractions) == 2 and details["recovery"]["attempts"] == 2
    assert len(observed) == (4 if media_type == "video" else 2)
    assert all(opts["videopassword"] == PASSWORD and opts["http_headers"]["User-Agent"] == USER_AGENT
               for opts in observed)
    assert not (tmp_path / "attempt-1").exists()
    assert PASSWORD not in str(details) and USER_AGENT not in str(details)


def test_direct_download_uses_only_validated_user_agent_in_guarded_transport(tmp_path, monkeypatch):
    headers_seen = []
    class Transport:
        def __init__(self, **kwargs): headers_seen.append(kwargs["headers"])
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, request):
            assert request.headers.get("Accept-Encoding") == "identity"
            return Response(io.BytesIO(b"data"), request.url, {"Content-Type": "video/mp4", "Content-Length": "4"})
    monkeypatch.setattr(engine, "PublicRH", Transport)
    path, details = engine.acquire_media("https://cdn.example.com/video.mp4", tmp_path, Guard(), "mp3",
                                         session=engine.SessionOptions(user_agent=USER_AGENT))
    assert path.read_bytes() == b"data"
    assert headers_seen == [{"User-Agent": USER_AGENT}]
    assert USER_AGENT not in str(details)


@pytest.mark.parametrize("function", [engine.inspect_media, engine.acquire_media, engine.acquire_video_media])
def test_video_password_is_not_sent_to_arbitrary_direct_downloads(function, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "direct_handler", lambda *_args, **_kwargs: pytest.fail("Password must not reach direct media"))
    args = ("https://cdn.example.com/video.mp4", Guard()) if function is engine.inspect_media else (
        "https://cdn.example.com/video.mp4", tmp_path, Guard(), "mp3" if function is engine.acquire_media else "source")
    with pytest.raises(AudioError) as error:
        function(*args, session=engine.SessionOptions(video_password=PASSWORD))
    assert error.value.code == "password_not_supported"
    assert PASSWORD not in error.value.message


def test_video_password_requires_https_before_any_platform_request(monkeypatch):
    monkeypatch.setattr(engine, "SafeYoutubeDL", lambda *_args, **_kwargs: pytest.fail("Password must not be sent through HTTP"))
    with pytest.raises(AudioError) as error:
        engine.inspect_media("http://vimeo.com/76979871", Guard(), session=engine.SessionOptions(video_password=PASSWORD))
    assert error.value.code == "password_https_required"


@pytest.mark.parametrize("message,code", [
    ("This video is protected by a password, use the --video-password option", "video_password_required"),
    ("Wrong password", "video_password_invalid"),
    ("Incorrect password", "video_password_invalid"),
])
def test_password_failure_stays_actionable_and_does_not_echo_upstream_details(message, code):
    error = engine.translate_error(RuntimeError(message + " " + PASSWORD), Guard())
    assert error.code == code and PASSWORD not in error.message


@pytest.mark.parametrize("code", ["video_password_required", "video_password_invalid",
                                   "password_not_supported", "password_https_required"])
def test_password_failures_never_resubmit_the_password_even_with_transient_causes(code, tmp_path, monkeypatch):
    calls = []
    def acquire(_url, _directory, _guard, _format, **kwargs):
        calls.append(kwargs["session"])
        failure = AudioError("Sanitized password failure", code)
        failure.__cause__ = RuntimeError("HTTP Error 503: " + PASSWORD)
        raise failure
    monkeypatch.setattr(engine, "acquire_media", acquire)
    with pytest.raises(AudioError) as error:
        engine.acquire_with_recovery(VIDEO_URL, tmp_path, "mp3", Guard(), engine.MediaSettings(), None,
                                     session=engine.SessionOptions(PASSWORD, USER_AGENT))
    assert len(calls) == 1 and error.value.code == code
    assert error.value.recovery_attempts == 1
    assert PASSWORD not in error.value.message
    assert recovery.recovery_metadata(error.value, attempt=1)["kind"] is None


def test_official_player_never_places_password_or_user_agent_in_frame_url():
    with TestClient(index.app) as client:
        response = client.post("/api/player", json={"url": VIDEO_URL, "cookies": COOKIE_EXPORT,
                                                   "video_password": PASSWORD, "user_agent": USER_AGENT})
    assert response.status_code == 200 and response.json()["kind"] == "embed"
    assert PASSWORD not in response.text and COOKIE_VALUE not in response.text and USER_AGENT not in response.text
    assert "senha diretamente no player" in response.json()["message"]


def test_extracted_preview_uses_session_for_extraction_then_checks_anonymous_playability(monkeypatch):
    observed, transport_headers = [], []
    class Extractor:
        def __init__(self, opts, guard, cookiejar=None): observed.append(opts)
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def extract_info(self, _url, download=False):
            return {"title": "Allowed preview", "formats": [{"url": "https://cdn.example.com/v.mp4", "protocol": "https",
                "ext": "mp4", "vcodec": "avc1", "acodec": "aac", "height": 720}]}
    class Transport:
        def __init__(self, guard): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def send(self, request):
            transport_headers.append(dict(request.headers))
            return Response(io.BytesIO(b""), request.url, {"Content-Type": "video/mp4", "Content-Length": "4"})
    monkeypatch.setattr(engine, "source_for", lambda _url: "Example platform")
    monkeypatch.setattr(engine, "SafeYoutubeDL", Extractor)
    monkeypatch.setattr(engine, "direct_handler", Transport)
    result = player.resolve_player("https://example.com/watch/1", "onda-audio.vercel.app", Guard(),
                                   session=engine.SessionOptions(PASSWORD, USER_AGENT))
    assert result["kind"] == "direct"
    assert observed[0]["videopassword"] == PASSWORD
    assert observed[0]["http_headers"]["User-Agent"] == USER_AGENT
    assert all("User-Agent" not in headers and "Cookie" not in headers for headers in transport_headers)
    assert PASSWORD not in str(result) and USER_AGENT not in str(result)
