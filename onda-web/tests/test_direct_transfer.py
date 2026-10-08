"""Real response/Guard tests for bounded recovery of owned direct media."""
import io
from pathlib import Path

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.exceptions import HTTPError

from api import direct_transfer, engine
from api.security import AudioError, Guard


URL = "https://media.example.com/owned-canary.mp4"
ETAG = '"owned-v1"'
MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


@pytest.fixture
def owned_media():
    return (Path(__file__).resolve().parents[1] / "public" / "canary-4k.mp4").read_bytes()


class Interrupted(io.BytesIO):
    def __init__(self, data, *, read_size=2048, fail=False, after_read=None):
        super().__init__(data)
        self.read_size = read_size
        self.fail = fail
        self.after_read = after_read

    def read(self, size=-1):
        result = super().read(min(size, self.read_size) if size >= 0 else self.read_size)
        if not result and self.fail:
            raise ConnectionResetError("origin interrupted owned canary transfer")
        if result and self.after_read:
            self.after_read()
        return result


def response(data, total=None, *, status=200, start=None, etag=ETAG, modified=None,
             fail=False, content_range=None, content_length=None, url=URL, content_type="video/mp4",
             after_read=None):
    total = len(data) if total is None else total
    headers = {"Content-Type": content_type, "Content-Length": str(total if status == 200 else total - start)}
    if etag is not None:
        headers["ETag"] = etag
    if modified is not None:
        headers["Last-Modified"] = modified
    if status == 206:
        headers["Content-Range"] = content_range or f"bytes {start}-{total - 1}/{total}"
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    return Response(Interrupted(data, fail=fail, after_read=after_read), url, headers, status)


def transport(monkeypatch, replies):
    requests = []

    def send(_handler, request):
        requests.append(request)
        reply = replies[len(requests) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    # Retain PublicRH._send, Response and BoundedResponse: guards and actual
    # byte accounting stay in the production path. Only socket IO is replaced.
    monkeypatch.setattr(UrllibRH, "_send", send)
    monkeypatch.setattr(direct_transfer, "wait_for_retry", lambda guard, _attempt, _exc=None: (guard.check() or True))
    return requests


def download(directory, guard):
    return direct_transfer.download_direct(URL, directory, guard,
        handler_factory=engine.direct_handler, verify_response=engine.verify_media_response,
        title_factory=engine.direct_title)


@pytest.mark.parametrize("fail", [False, True], ids=["short-eof", "connection-reset"])
def test_interrupted_owned_media_resumes_identical_bytes(monkeypatch, tmp_path, owned_media, fail):
    prefix = 8192
    requests = transport(monkeypatch, [response(owned_media[:prefix], len(owned_media), fail=fail),
        response(owned_media[prefix:], len(owned_media), status=206, start=prefix)])
    guard = Guard(maximum_bytes=len(owned_media))
    original_started = guard.started
    path, metadata = download(tmp_path, guard)
    assert path.read_bytes() == owned_media
    assert requests[1].headers["Range"] == f"bytes={prefix}-"
    assert requests[1].headers["If-Range"] == ETAG
    assert guard.received == len(owned_media)
    assert guard.started == original_started
    assert metadata["recovery"] == {"attempts": 2, "resumed": True, "method": "direct_http"}


@pytest.mark.parametrize("etag,modified", [(None, MODIFIED), ('W/"owned-v1"', MODIFIED)])
def test_valid_last_modified_can_resume_without_strong_etag(monkeypatch, tmp_path, owned_media, etag, modified):
    prefix = 4096
    requests = transport(monkeypatch, [response(owned_media[:prefix], len(owned_media), etag=etag, modified=modified),
        response(owned_media[prefix:], len(owned_media), status=206, start=prefix, etag=etag, modified=modified)])
    path, _ = download(tmp_path, Guard())
    assert path.read_bytes() == owned_media
    assert requests[1].headers["If-Range"] == modified


@pytest.mark.parametrize("etag,modified", [(None, None), ('W/"weak-v1"', None), ('bad\r\nvalue', None),
                                          (None, "yesterday"), (None, "Wed, 21 Oct 2015 07:28:00")])
def test_unverifiable_partial_restarts_instead_of_appending(monkeypatch, tmp_path, owned_media, etag, modified):
    prefix = 4096
    requests = transport(monkeypatch, [response(owned_media[:prefix], len(owned_media), etag=etag, modified=modified),
        response(owned_media, etag=etag, modified=modified)])
    guard = Guard()
    path, metadata = download(tmp_path, guard)
    assert path.read_bytes() == owned_media
    assert "Range" not in requests[1].headers and "If-Range" not in requests[1].headers
    assert guard.received == prefix + len(owned_media)
    assert metadata["recovery"]["resumed"] is False


def test_server_ignoring_range_safely_replaces_partial(monkeypatch, tmp_path, owned_media):
    prefix = 4096
    new_version = owned_media[::-1]
    requests = transport(monkeypatch, [response(owned_media[:prefix], len(owned_media)),
        response(new_version, etag='"owned-v2"')])
    guard = Guard()
    path, metadata = download(tmp_path, guard)
    assert requests[1].headers["Range"] == f"bytes={prefix}-"
    assert path.read_bytes() == new_version
    assert guard.received == prefix + len(new_version)
    assert metadata["recovery"] == {"attempts": 2, "resumed": False, "method": "direct_http"}


@pytest.mark.parametrize("bad", ["start", "total", "etag", "missing-etag", "missing-range", "length", "redirect"])
def test_invalid_range_never_appends_and_next_attempt_is_full(monkeypatch, tmp_path, owned_media, bad):
    prefix = 4096
    kwargs = {"status": 206, "start": prefix}
    if bad == "start":
        kwargs["content_range"] = f"bytes {prefix + 1}-{len(owned_media) - 1}/{len(owned_media)}"
    elif bad == "total":
        kwargs["content_range"] = f"bytes {prefix}-{len(owned_media)}/{len(owned_media) + 1}"
        kwargs["content_length"] = len(owned_media) - prefix + 1
    elif bad == "etag":
        kwargs["etag"] = '"changed-version"'
    elif bad == "missing-etag":
        kwargs["etag"] = None
    elif bad == "missing-range":
        kwargs["content_range"] = "invalid"
    elif bad == "length":
        kwargs["content_length"] = len(owned_media) - prefix + 10
    elif bad == "redirect":
        kwargs["url"] = "https://other.example.com/owned-canary.mp4"
    requests = transport(monkeypatch, [response(owned_media[:prefix], len(owned_media)),
        response(b"CORRUPTED" * 100, len(owned_media), **kwargs), response(owned_media)])
    guard = Guard()
    path, metadata = download(tmp_path, guard)
    assert path.read_bytes() == owned_media
    assert len(requests) == 3 and "Range" not in requests[2].headers
    assert guard.received == prefix + len(owned_media)
    assert metadata["recovery"]["attempts"] == 3
    assert metadata["recovery"]["resumed"] is False


def test_three_truncated_responses_fail_and_remove_partial(monkeypatch, tmp_path, owned_media):
    requests = transport(monkeypatch, [response(owned_media[:4096], len(owned_media)),
        response(owned_media[4096:8192], len(owned_media), status=206, start=4096),
        response(owned_media[8192:12288], len(owned_media), status=206, start=8192)])
    guard = Guard()
    with pytest.raises(AudioError) as failure:
        download(tmp_path, guard)
    assert failure.value.code == "download_incomplete"
    assert failure.value.recovery_exhausted is True
    assert failure.value.recovery_attempts == 3
    assert len(requests) == 3 and guard.received == 12288
    assert not (tmp_path / "source.media").exists()


def test_retry_can_never_reset_aggregate_network_byte_budget(monkeypatch, tmp_path, owned_media):
    prefix = 8192
    requests = transport(monkeypatch, [response(owned_media[:prefix], len(owned_media), etag=None), response(owned_media)])
    guard = Guard(maximum_bytes=len(owned_media) + prefix - 1)
    with pytest.raises(AudioError) as failure:
        download(tmp_path, guard)
    assert failure.value.code == "source_too_large"
    assert len(requests) == 2 and guard.received == prefix
    assert not (tmp_path / "source.media").exists()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_temporary_http_failure_recovers_with_guarded_transport(monkeypatch, tmp_path, owned_media, status):
    failed = HTTPError(Response(io.BytesIO(b""), URL, {"Retry-After": "0"}, status))
    requests = transport(monkeypatch, [failed, response(owned_media)])
    guard = Guard()
    path, metadata = download(tmp_path, guard)
    assert path.read_bytes() == owned_media and len(requests) == 2
    assert failed.response.closed
    assert guard.received == len(owned_media)
    assert metadata["recovery"]["attempts"] == 2


def test_full_replacement_after_partial_resume_does_not_report_resumed_output(monkeypatch, tmp_path, owned_media):
    transport(monkeypatch, [response(owned_media[:4096], len(owned_media)),
        response(owned_media[4096:8192], len(owned_media), status=206, start=4096), response(owned_media)])
    guard = Guard()
    path, metadata = download(tmp_path, guard)
    assert path.read_bytes() == owned_media and guard.received == 8192 + len(owned_media)
    assert metadata["recovery"] == {"attempts": 3, "resumed": False, "method": "direct_http"}


def test_unknown_length_body_is_bounded_by_aggregate_guard(monkeypatch, tmp_path, owned_media):
    reply = response(owned_media)
    del reply.headers["Content-Length"]
    requests = transport(monkeypatch, [reply])
    guard = Guard(maximum_bytes=4095)
    with pytest.raises(AudioError) as failure:
        download(tmp_path, guard)
    assert failure.value.code == "source_too_large"
    assert len(requests) == 1 and guard.received == 4096
    assert not (tmp_path / "source.media").exists()


def test_cancellation_during_read_does_not_retry(monkeypatch, tmp_path, owned_media):
    guard = Guard()
    requests = transport(monkeypatch, [response(owned_media, after_read=guard.abort)])
    with pytest.raises(AudioError) as failure:
        download(tmp_path, guard)
    assert failure.value.code == "cancelled" and len(requests) == 1
    assert not (tmp_path / "source.media").exists()


def test_deadline_is_not_reset_for_recovery(monkeypatch, tmp_path, owned_media):
    guard = Guard(seconds=240)
    requests = transport(monkeypatch, [response(owned_media[:4096], len(owned_media))])

    def expire(original_guard, _attempt, _exc=None):
        original_guard.started -= 241
        original_guard.check()

    monkeypatch.setattr(direct_transfer, "wait_for_retry", expire)
    with pytest.raises(AudioError) as failure:
        download(tmp_path, guard)
    assert failure.value.code == "timeout" and len(requests) == 1
    assert not (tmp_path / "source.media").exists()


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_permanent_http_failure_is_not_retried(monkeypatch, tmp_path, status):
    requests = transport(monkeypatch, [HTTPError(Response(io.BytesIO(b""), URL, {}, status))])
    with pytest.raises(HTTPError):
        download(tmp_path, Guard())
    assert len(requests) == 1 and not (tmp_path / "source.media").exists()


def test_html_error_page_is_never_saved_or_retried(monkeypatch, tmp_path):
    requests = transport(monkeypatch, [response(b"<html>login required</html>", content_type="text/html")])
    guard = Guard()
    with pytest.raises(AudioError) as failure:
        download(tmp_path, guard)
    assert failure.value.code == "not_media"
    assert len(requests) == 1 and guard.received == 0
    assert not (tmp_path / "source.media").exists()


def test_response_verification_runs_before_every_attempt(monkeypatch, tmp_path, owned_media):
    transport(monkeypatch, [response(owned_media[:4096], len(owned_media)),
        response(owned_media[4096:], len(owned_media), status=206, start=4096)])
    observed = []

    def verify(reply, response_url):
        observed.append((reply.status, response_url, reply.fp.fp.tell()))
        engine.verify_media_response(reply, response_url)

    direct_transfer.download_direct(URL, tmp_path, Guard(), handler_factory=engine.direct_handler,
        verify_response=verify, title_factory=engine.direct_title)
    assert observed == [(200, URL, 0), (206, URL, 0)]
