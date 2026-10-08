"""Recovery must preserve aggregate limits, cancellation, and access controls."""
import errno
import io
import socket
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError

from api import recovery
from api.recovery import AttemptGuard, MAX_ATTEMPTS, recovery_metadata, retry_kind, wait_for_retry
from api.security import AudioError, Guard


class Socket:
    def __init__(self):
        self.closed = threading.Event()

    def shutdown(self, _how):
        self.closed.set()

    def close(self):
        self.closed.set()


def test_retry_attempts_share_original_byte_and_deadline_budget():
    parent = Guard(maximum_bytes=10, seconds=12)
    first = AttemptGuard(parent, attempt=1)
    first.count(6)
    first.error = AudioError("Source connection lost", "download_failed")
    first.abort()
    second = AttemptGuard(parent, attempt=2, profile="alternate", excluded_formats={"high"})
    assert second.received == 6
    assert second.started == parent.started
    assert second.seconds == parent.seconds
    assert second.remaining <= first.remaining + .01
    assert second.maximum_bytes == 10
    assert second.excluded_formats == frozenset({"high"})
    second.count(4)
    with pytest.raises(AudioError) as over_budget:
        second.count(1)
    assert over_budget.value is parent.error
    assert over_budget.value.code == "source_too_large"
    assert parent.received == 11
    assert parent.cancelled.is_set()
    third = AttemptGuard(parent, attempt=3)
    with pytest.raises(AudioError) as still_original:
        third.check()
    assert still_original.value is over_budget.value


def test_attempt_abort_closes_its_peers_but_allows_another_attempt():
    parent = Guard()
    first, second = AttemptGuard(parent), AttemptGuard(parent, attempt=2)
    first_socket, second_socket = Socket(), Socket()
    first.register_socket(first_socket)
    second.register_socket(second_socket)
    assert parent.sockets == {first_socket, second_socket}
    first.abort()
    assert first_socket.closed.is_set()
    assert not second_socket.closed.is_set()
    assert not parent.cancelled.is_set()
    assert parent.sockets == {second_socket}
    second.check()
    with pytest.raises(AudioError) as cancelled:
        first.check()
    assert cancelled.value.code == "cancelled"
    parent.abort()
    assert second_socket.closed.is_set()
    with pytest.raises(AudioError) as cancelled:
        second.check()
    assert cancelled.value.code == "cancelled"


def test_master_error_wins_over_local_parallel_failure_and_closes_every_socket():
    parent = Guard(maximum_bytes=3)
    first, second = AttemptGuard(parent), AttemptGuard(parent, attempt=2)
    sockets = [Socket(), Socket()]
    first.register_socket(sockets[0])
    second.register_socket(sockets[1])
    first.error = AudioError("Transient attempt error", "download_failed")
    with pytest.raises(AudioError) as full:
        second.count(4)
    assert all(connection.closed.is_set() for connection in sockets)
    with pytest.raises(AudioError) as original:
        first.check()
    assert original.value is full.value


def test_registration_after_master_or_attempt_cancellation_closes_socket():
    for master in (False, True):
        parent = Guard()
        attempt = AttemptGuard(parent)
        (parent if master else attempt).abort()
        connection = Socket()
        with pytest.raises(AudioError):
            attempt.register_socket(connection)
        assert connection.closed.is_set()
        assert not attempt.sockets
        assert not parent.sockets


def test_parallel_attempt_count_cannot_reset_or_race_aggregate_budget():
    parent = Guard(maximum_bytes=100)
    attempts = [AttemptGuard(parent, attempt=n + 1) for n in range(MAX_ATTEMPTS)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda attempt: [attempt.count(1) for _ in range(30)], attempts))
    assert parent.received == 90
    with pytest.raises(AudioError) as raised:
        attempts[-1].count(11)
    assert raised.value.code == "source_too_large"
    assert parent.received == 101


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"), ConnectionResetError("reset"),
    ConnectionAbortedError("aborted"), socket.gaierror(-3, "temporary DNS failure"),
    OSError(errno.EPIPE, "pipe"), OSError(errno.ENETUNREACH, "network"),
    AudioError("Origin timed out", "upstream_timeout", 504),
    AudioError("Origin DNS failure", "dns_failed"),
    urllib.error.HTTPError("https://public.example/media", 429, "slow down", {}, None),
    urllib.error.HTTPError("https://public.example/media", 408, "request timeout", {}, None),
    urllib.error.HTTPError("https://public.example/media", 503, "busy", {}, None),
    RuntimeError("HTTP Error 500: Server error"),
    TransportError(cause=TimeoutError("upstream timed out")),
])
def test_only_transport_temporary_failures_are_transient(error):
    assert retry_kind(error) == "transient"


@pytest.mark.parametrize("code", [
    "cancelled", "timeout", "deadline_exceeded", "source_too_large", "output_too_large",
    "metadata_too_large", "unsafe_url", "invalid_url", "invalid_trim", "auth_required",
    "invalid_cookies", "cookies_expired", "unsupported_source", "playlist", "live_video",
])
def test_safety_deadline_and_permission_errors_never_retry_even_with_raw_timeout(code):
    error = AudioError("Final request error", code)
    error.__cause__ = TimeoutError("origin timed out")
    error.recovery_phase = "transfer"
    assert retry_kind(error) is None


@pytest.mark.parametrize("message", [
    "Sign in to confirm you're not a bot", "This is a private video",
    "The content is members-only", "Video requires login", "This item is private",
    "Unsupported DRM protection", "This video has been removed",
    "Blocked due to copyright", "Authentication required", "HTTP Error 401: Unauthorized",
])
def test_access_restrictions_never_try_to_bypass_or_repeat(message):
    error = AudioError("Platform blocked", "platform_blocked")
    error.__cause__ = RuntimeError(message)
    error.recovery_phase = "transfer"
    assert retry_kind(error) is None


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("phase, expected", [("transfer", "refresh"), ("extract", None), (None, None)])
def test_refreshes_expired_transfer_urls_only(status, phase, expected):
    raw = urllib.error.HTTPError("https://cdn.example/expired?token=secret", status,
                                 "Forbidden" if status == 403 else "Not found", {}, None)
    error = AudioError("A plataforma exige login" if status == 403 else "Conteúdo removido",
                       "platform_blocked" if status == 403 else "unavailable")
    error.__cause__ = raw
    error.recovery_phase = phase
    assert retry_kind(error) == expected


def test_ytdlp_http_error_is_recognized_through_its_download_error_wrapper():
    response = Response(io.BytesIO(b"busy"), "https://cdn.example/media", {}, status=503)
    raw = HTTPError(response)
    download = DownloadError("ERROR: origin failed", exc_info=(HTTPError, raw, None))
    error = AudioError("Origin failed", "upstream_error")
    error.__cause__ = download
    assert retry_kind(error) == "transient"


@pytest.mark.parametrize("code", ["platform_blocked", "unavailable"])
def test_explicit_block_or_removal_stays_final_without_evidence_of_expired_transfer(code):
    error = AudioError("Final platform response", code)
    error.__cause__ = TimeoutError("Unrelated network detail")
    error.recovery_phase = "transfer"
    assert retry_kind(error) is None


@pytest.mark.parametrize("code", ["format_unavailable", "download_failed", "incomplete", "unsupported_transfer"])
def test_unsupported_representation_can_select_an_alternative(code):
    assert retry_kind(AudioError("Unavailable representation", code)) == "alternate"


def test_raw_format_error_survives_generic_no_audio_translation():
    error = AudioError("No audio", "no_audio")
    error.__cause__ = RuntimeError("Requested format is not available")
    assert retry_kind(error) == "alternate"


@pytest.mark.parametrize("code", ["download_incomplete", "invalid_media_response"])
def test_direct_incomplete_transfer_can_resume(code):
    error = AudioError("Origin did not send the complete file", code, 502)
    error.recovery_phase = "transfer"
    assert retry_kind(error) == "transient"
    error.recovery_exhausted = True
    assert retry_kind(error) is None


def test_exception_graph_is_bounded_and_does_not_loop_on_cyclic_causes():
    first, second = RuntimeError("origin"), TimeoutError("timeout")
    first.__cause__ = second
    second.__cause__ = first
    assert retry_kind(first) == "transient"
    assert len(recovery._causes(first)) == 2
    current = first = RuntimeError("origin")
    for _ in range(20):
        current.__cause__ = RuntimeError("origin")
        current = current.__cause__
    assert len(recovery._causes(first)) == 8


class ClockGuard:
    def __init__(self, seconds=10):
        self.now = 0.0
        self.seconds = seconds
        self.waits = []
        self.cancelled = self

    @property
    def remaining(self):
        return self.seconds - self.now

    def check(self):
        if self.remaining <= 0:
            raise AudioError("Original deadline", "timeout", 504)

    def wait(self, delay):
        self.waits.append(delay)
        self.now += delay
        return False


@pytest.mark.parametrize("attempt, expected", [(1, .3), (2, .7), (3, .7)])
def test_backoff_is_short_and_checks_every_tenth_of_a_second(monkeypatch, attempt, expected):
    guard = ClockGuard()
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: guard.now))
    assert wait_for_retry(guard, attempt)
    assert guard.now == pytest.approx(expected)
    assert guard.waits and max(guard.waits) <= .1


@pytest.mark.parametrize("value, expected", [("2", 2), ("3600", 3), ("-1", 0), ("nan", .3), ("invalid", .3)])
def test_retry_after_is_respected_but_capped_and_validated(monkeypatch, value, expected):
    guard = ClockGuard()
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: guard.now))
    error = urllib.error.HTTPError("https://public.example/media", 429, "busy",
                                   {"Retry-After": value}, None)
    translated = AudioError("Origin busy", "upstream_error")
    translated.__cause__ = error
    assert wait_for_retry(guard, 1, translated)
    assert guard.now == pytest.approx(expected)
    assert not guard.waits or max(guard.waits) <= .1


def test_retry_after_http_date_is_capped(monkeypatch):
    guard = ClockGuard()
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: guard.now))
    error = urllib.error.HTTPError("https://public.example/media", 429, "busy",
                                   {"Retry-After": formatdate(time.time() + 60, usegmt=True)}, None)
    assert wait_for_retry(guard, 1, error)
    assert guard.now == pytest.approx(3)


def test_retry_stops_if_not_enough_time_to_wait_and_perform_another_attempt(monkeypatch):
    guard = ClockGuard(seconds=1.2)
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: guard.now))
    assert not wait_for_retry(guard, 1)
    assert not guard.waits
    assert guard.now == 0
    guard.seconds = 0
    with pytest.raises(AudioError) as expired:
        wait_for_retry(guard, 1)
    assert expired.value.code == "timeout"


def test_master_cancel_interrupts_waiting_attempt_quickly():
    parent = Guard()
    attempt = AttemptGuard(parent, attempt=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        started = time.monotonic()
        waiting = pool.submit(wait_for_retry, attempt, 2)
        time.sleep(.03)
        parent.abort()
        with pytest.raises(AudioError) as cancelled:
            waiting.result(timeout=.4)
    assert cancelled.value.code == "cancelled"
    assert time.monotonic() - started < .5


def test_reports_never_include_messages_urls_headers_secrets_or_unknown_profiles():
    raw = urllib.error.HTTPError("https://cdn.example/file?token=top-secret", 503,
                                 "secret upstream message", {"Authorization": "Bearer credential"}, None)
    error = AudioError("secret user message", "https://secret.example/token")
    error.__cause__ = raw
    report = recovery_metadata(error, attempt=900, profile="Bearer secret")
    assert report == {"attempt": 3, "profile": "default", "code": "upstream_error", "kind": "transient"}
    assert "secret" not in str(report)
    assert recovery_metadata(AudioError("anything", "download_failed"), attempt=0,
                             profile="alternate")["code"] == "download_failed"
