"""Bounded recovery that keeps the request's original safety and byte budgets.

An attempt can stop its parallel peers without cancelling the whole request.
This module never follows a different URL, changes an authorization requirement,
or records an upstream message: callers remain responsible for public transport
validation and for selecting a supported alternative representation.
"""
from __future__ import annotations

import errno
import http.client
import math
import re
import socket
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .security import AudioError, Guard
from yt_dlp.networking.exceptions import IncompleteRead


MAX_ATTEMPTS = 3
MIN_RETRY_REMAINING = 1.0
MAX_RETRY_AFTER = 3.0
_CHAIN_LIMIT = 8


class AttemptGuard:
    """Isolate a failed attempt while sharing the parent deadline and byte count."""

    def __init__(self, parent: Guard, *, attempt: int = 1, profile: str = "default",
                 excluded_formats=frozenset(), force_encode: bool = False):
        self.parent = parent
        self.attempt = attempt
        self.profile = profile
        self.excluded_formats = frozenset(excluded_formats)
        self.force_encode = force_encode
        self.error: AudioError | None = None
        self.cancelled = threading.Event()
        self.sockets = set()
        self.socket_lock = threading.Lock()
        self.byte_lock = threading.Lock()

    @property
    def remaining(self):
        return self.parent.remaining

    @property
    def seconds(self):
        return self.parent.seconds

    @property
    def started(self):
        return self.parent.started

    @property
    def maximum_bytes(self):
        return self.parent.maximum_bytes

    @property
    def received(self):
        return self.parent.received

    def check(self):
        # A master cancellation, deadline, or aggregate-budget failure always
        # wins over the local error that caused a representation to be retried.
        self.parent.check()
        if self.error:
            raise self.error
        if self.cancelled.is_set():
            raise AudioError("Download cancelado.", "cancelled", 499)

    def count(self, size: int):
        self.check()
        self.parent.count(size)

    def register_socket(self, connection):
        # Register before checking so a socket connected concurrently with
        # cancellation is still closed. The parent can interrupt every attempt.
        with self.socket_lock:
            self.sockets.add(connection)
        try:
            self.parent.register_socket(connection)
            self.check()
        except BaseException:
            self.close_sockets()
            raise

    def close_sockets(self):
        with self.socket_lock:
            connections, self.sockets = self.sockets, set()
        with self.parent.socket_lock:
            self.parent.sockets.difference_update(connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def abort(self):
        self.cancelled.set()
        self.close_sockets()


def _causes(exc):
    """Read a small exception graph, including yt-dlp's explicit raw causes."""
    pending, seen, chain = [exc], set(), []
    while pending and len(chain) < _CHAIN_LIMIT:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.append(current.__cause__)
        pending.append(getattr(current, "cause", None))
        info = getattr(current, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1:
            pending.append(info[1])
    return chain


_NEVER_CODES = frozenset({
    "cancelled", "timeout", "deadline", "deadline_exceeded",
    "source_too_large", "output_too_large", "metadata_too_large",
    "unsafe_url", "invalid_url", "invalid_source", "invalid_trim",
    "invalid_cookies", "cookie_domain", "cookie_https_required",
    "cookies_expired", "cookies_not_supported", "auth_required",
    "video_password_required", "video_password_invalid", "password_not_supported", "password_https_required",
    "authentication_required", "auth_forbidden", "auth_not_configured",
    "auth_unavailable", "login_required", "cookies_required", "private",
    "drm", "drm_protected", "removed", "unsupported_source",
    "playlist", "live_video", "not_media", "unavailable_resolution",
})
_TRANSIENT_CODES = frozenset({"upstream_timeout", "upstream_unavailable", "upstream_rate_limited",
                              "dns_failed", "rate_limited"})
_ALTERNATE_CODES = frozenset({
    "format_unavailable", "no_audio", "no_video", "download_failed", "incomplete", "unsupported_transfer",
})
_BLOCKED_TEXT = (
    "sign in", "sign-in", "signin", "log in", "login", "not a bot",
    "private", "members-only", "members only",
    "authentication required", "authorization required", "unauthorized",
    "age-restricted", "age restricted", "drm", "digital rights",
    "removed",
    "deleted by", "copyright", "geo-restricted", "not available in your country",
)
_STATUS_PATTERN = re.compile(r"(?:http(?:\s+error|\s+status)?|status(?:\s+code)?)\s*[:=]?\s*(\d{3})\b", re.I)


def _status(exc):
    for name in ("status", "status_code", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    match = _STATUS_PATTERN.search(str(exc)[:4096])
    return int(match[1]) if match else None


def retry_kind(exc) -> str | None:
    """Classify useful recovery only; permission and safety failures stay final."""
    chain = _causes(exc)
    if not chain or any(getattr(item, "recovery_exhausted", False) for item in chain):
        return None
    codes = {getattr(item, "code", None) for item in chain if isinstance(item, AudioError)}
    if codes & _NEVER_CODES or any(isinstance(code, str) and code.startswith("invalid_")
                                  and code != "invalid_media_response" for code in codes):
        return None
    # translate_error's user-facing wrapper can say "login" for an expired
    # transfer URL. Inspect the actual source rather than that generic message.
    originals = [item for item in chain if not isinstance(item, AudioError)]
    messages = originals or chain
    texts = [str(item).lower()[:4096] for item in messages]
    if any(token in text for text in texts for token in _BLOCKED_TEXT):
        return None
    phase = next((getattr(item, "recovery_phase", None) for item in chain
                  if getattr(item, "recovery_phase", None)), None)
    statuses = [_status(item) for item in originals or chain]
    if 401 in statuses:
        return None
    if any(status in (403, 404) for status in statuses):
        return "refresh" if phase == "transfer" else None
    if codes & {"platform_blocked", "unavailable"}:
        return None
    if any(status in (408, 429) or status is not None and 500 <= status <= 599 for status in statuses):
        return "transient"
    if codes & _TRANSIENT_CODES:
        return "transient"
    if phase == "transfer" and codes & {"download_incomplete", "invalid_media_response"}:
        return "transient"
    transient_errnos = {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
                        errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPIPE}
    if any(isinstance(item, (TimeoutError, ConnectionError, socket.gaierror, http.client.IncompleteRead, IncompleteRead))
           or isinstance(item, OSError) and item.errno in transient_errnos for item in chain):
        return "transient"
    if any(token in text for text in texts for token in (
        "timed out", "timeout", "connection reset", "connection aborted",
        "temporary failure in name resolution", "name or service not known",
        "remote end closed connection", "connection refused",
    )):
        return "transient"
    if codes & _ALTERNATE_CODES or any(token in text for text in texts for token in (
        "requested format is not available", "requested format not available",
        "no matching formats", "no video formats", "incomplete download",
        "download incomplete", "unsupported transfer",
    )):
        return "alternate"
    return None


def _retry_after(exc):
    for item in _causes(exc):
        response = getattr(item, "response", None)
        for headers in (getattr(item, "headers", None), getattr(response, "headers", None)):
            if not hasattr(headers, "get"):
                continue
            value = headers.get("Retry-After") or headers.get("retry-after")
            if value is None:
                continue
            try:
                delay = float(value)
            except (TypeError, ValueError):
                try:
                    date = parsedate_to_datetime(str(value))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    delay = (date - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    continue
            if math.isfinite(delay):
                return min(MAX_RETRY_AFTER, max(0.0, delay))
    return None


def wait_for_retry(guard, attempt: int, exc=None) -> bool:
    """Wait briefly, interruptibly, and only if there is time for another try."""
    guard.check()
    delay = _retry_after(exc) if exc is not None else None
    if delay is None:
        delay = 0.3 if attempt <= 1 else 0.7
    if guard.remaining < delay + MIN_RETRY_REMAINING:
        return False
    end = time.monotonic() + delay
    while True:
        guard.check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return guard.remaining >= MIN_RETRY_REMAINING
        guard.cancelled.wait(min(0.1, remaining))


_REPORT_CODES = _NEVER_CODES | _TRANSIENT_CODES | _ALTERNATE_CODES | frozenset({
    "platform_blocked", "unavailable", "upstream_error", "no_audio", "no_video",
    "conversion_failed", "download_incomplete", "invalid_media_response",
})
_REPORT_PROFILES = frozenset({
    "default", "refresh", "refreshed", "alternate", "compatible", "safe_encode",
    "encode", "direct", "retry", "conservative",
})


def recovery_metadata(exc, *, attempt: int, profile: str = "default") -> dict:
    """Return bounded codes only; never include URLs, headers, or error text."""
    code = next((getattr(item, "code", None) for item in _causes(exc)
                 if getattr(item, "code", None) in _REPORT_CODES), "upstream_error")
    return {"attempt": max(1, min(MAX_ATTEMPTS, int(attempt))),
            "profile": profile if profile in _REPORT_PROFILES else "default",
            "code": code, "kind": retry_kind(exc)}
