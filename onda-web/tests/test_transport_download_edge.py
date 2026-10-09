"""Regressions for representation-safe resume and redirect credential scope."""
import io
from pathlib import Path

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.exceptions import HTTPError

from api import direct_transfer, engine, security
from api.security import Guard, PublicRedirectHandler


URL = "https://media.example.com/owned-canary.mp4"
MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"
OLD_DATE = "Wed, 21 Oct 2015 07:29:00 GMT"


def _response(data, total, *, start=None, modified=MODIFIED, date=None, etag=None):
    headers = {"Content-Type": "video/mp4", "Content-Length": str(len(data) if start is not None else total)}
    if modified is not None:
        headers["Last-Modified"] = modified
    if date is not None:
        headers["Date"] = date
    if etag is not None:
        headers["ETag"] = etag
    if start is not None:
        headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
    return Response(io.BytesIO(data), URL, headers, 206 if start is not None else 200)


@pytest.mark.parametrize("date,etag", [
    (None, None), (MODIFIED, None), ("Wed, 21 Oct 2015 07:28:59 GMT", None),
    ("invalid-date", None), ("Wed, 21 Oct 2015 07:27:00 GMT", None),
    (OLD_DATE, 'W/"same-second-version"'),
])
def test_weak_time_validator_never_splices_two_versions(monkeypatch, tmp_path, date, etag):
    original = (Path(__file__).resolve().parents[1] / "public" / "canary-4k.mp4").read_bytes()
    # Keep the container/length unchanged but change a payload byte inside the
    # already-received prefix, as a source can do twice within one second.
    prefix = 4096
    changed = bytearray(original)
    changed[2048] ^= 1
    changed = bytes(changed)
    requests = []

    def send(_handler, request):
        requests.append(request)
        if len(requests) == 1:
            return _response(original[:prefix], len(original), date=date, etag=etag)
        start = prefix if "Range" in request.headers else None
        return _response(changed[prefix:] if start is not None else changed, len(changed),
                         start=start, date=date, etag=etag)

    monkeypatch.setattr(UrllibRH, "_send", send)
    monkeypatch.setattr(direct_transfer, "wait_for_retry", lambda guard, *_args: guard.check() or True)
    guard = Guard()
    path, details = engine.direct_download(URL, tmp_path, guard)
    assert path.read_bytes() == changed
    assert "Range" not in requests[1].headers and "If-Range" not in requests[1].headers
    assert guard.received == prefix + len(changed)
    assert details["recovery"]["resumed"] is False


@pytest.mark.parametrize("target", [
    "http://media.example.com/owned-canary.mp4",
    "https://other.example.com/owned-canary.mp4",
])
def test_redirect_removes_authorization_when_origin_changes(monkeypatch, target):
    monkeypatch.setattr(security, "public_url", lambda *_args, **_kwargs: ("media.example.com", ["93.184.216.34"]))
    request = security.urllib.request.Request(URL, headers={"Authorization": "Bearer owned-test-session"})
    redirected = PublicRedirectHandler(Guard()).redirect_request(request, None, 302, "Found", {}, target)
    assert redirected.get_header("Authorization") is None


def test_same_origin_redirect_keeps_authorization(monkeypatch):
    monkeypatch.setattr(security, "public_url", lambda *_args, **_kwargs: ("media.example.com", ["93.184.216.34"]))
    request = security.urllib.request.Request(URL, headers={"Authorization": "Bearer owned-test-session"})
    redirected = PublicRedirectHandler(Guard()).redirect_request(
        request, None, 302, "Found", {}, "https://media.example.com/next.mp4")
    assert redirected.get_header("Authorization") == "Bearer owned-test-session"


@pytest.mark.parametrize("status", [405, 501])
def test_direct_inspection_uses_bounded_get_when_head_is_unsupported(monkeypatch, status):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    failure = HTTPError(Response(io.BytesIO(b"method unavailable"), URL, {}, status))
    requests = []

    def send(_handler, request):
        requests.append(request)
        if request.method == "HEAD":
            raise failure
        return _response(b"\0", 1, start=0)

    monkeypatch.setattr(UrllibRH, "_send", send)
    result = engine.inspect_media(URL, Guard())
    assert result["source"] == "Arquivo direto"
    assert [request.method for request in requests] == ["HEAD", "GET"]
    assert requests[1].headers["Range"] == "bytes=0-0"
    assert failure.response.closed


def test_direct_inspection_does_not_retry_auth_error_containing_405(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (security.socket.AF_INET, security.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    requests = []

    def send(_handler, request):
        requests.append(request)
        raise HTTPError(Response(io.BytesIO(), URL, {}, 401, "Authentication required for media 405"))

    monkeypatch.setattr(UrllibRH, "_send", send)
    with pytest.raises(security.AudioError) as failure:
        engine.inspect_media(URL, Guard())
    assert failure.value.code == "platform_blocked"
    assert len(requests) == 1
