"""Bounded HTTP transfer recovery through the existing public-only transport.

Only an unchanged representation may be resumed. All attempts share the
original Guard: neither elapsed time nor bytes already received are reset.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
import re

from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError

from .security import AudioError
from .recovery import MAX_ATTEMPTS, retry_kind, wait_for_retry


CHUNK_SIZE = 64 * 1024
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z", re.IGNORECASE)
_STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"\Z')


@dataclass(frozen=True)
class _Representation:
    url: str
    validator: tuple[str, str] | None
    total: int | None


def _validator(headers):
    etag = headers.get("ETag")
    if etag is not None:
        etag = etag.strip()
        if len(etag) <= 512 and _STRONG_ETAG.fullmatch(etag):
            return "ETag", etag
        # If-Range cannot use a weak entity tag, nor substitute a date when an
        # entity tag is present. Restart instead of splicing uncertain bytes.
        return None
    modified = headers.get("Last-Modified", "").strip()
    dated = headers.get("Date", "").strip()
    if any(not value or len(value) > 128 or "\r" in value or "\n" in value for value in (modified, dated)):
        return None
    try:
        modified_date = parsedate_to_datetime(modified)
        response_date = parsedate_to_datetime(dated)
        # HTTP dates have only one-second precision. A client may infer a
        # strong Last-Modified validator only when the stored response's Date
        # is at least 60 seconds later (RFC 9110, section 8.8.2.2).
        if (all(date.tzinfo is not None and date.utcoffset().total_seconds() == 0
                for date in (modified_date, response_date))
                and (response_date - modified_date).total_seconds() >= 60):
            return "Last-Modified", modified
    except (TypeError, ValueError, OverflowError, AttributeError):
        pass
    return None


def _transfer_error(message, code="invalid_media_response"):
    error = AudioError(message, code, 502)
    error.recovery_phase = "transfer"
    return error


def _length(headers):
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    value = raw.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise _transfer_error("A origem retornou um tamanho de arquivo inválido.")
    return int(value)


def _range(headers):
    raw = headers.get("Content-Range", "").strip()
    match = _CONTENT_RANGE.fullmatch(raw) if len(raw) <= 80 else None
    if not match:
        raise _transfer_error("A origem retornou uma retomada de arquivo inválida.")
    start, end, total = map(int, match.groups())
    if not 0 <= start <= end < total:
        raise _transfer_error("A origem retornou uma retomada de arquivo inválida.")
    return start, end, total


def _discard(source):
    with suppress(OSError):
        source.unlink()


def download_direct(url, directory, guard, *, handler_factory, verify_response, title_factory):
    """Download one public media resource, with at most three HTTP attempts.

    ``handler_factory`` must return the existing guarded PublicRH (or its test
    equivalent). It accounts for every response read, including redownloads;
    this function never substitutes an unguarded networking implementation.
    """
    source = Path(directory) / "source.media"
    representation = None
    resumed = False
    checkpoint = getattr(guard, "partial_checkpoint", None)
    retained = False
    if checkpoint and checkpoint.get("requestHash") == hashlib.sha256(url.encode()).hexdigest() and source.is_file():
        if type(checkpoint.get("size")) is int and 0 < checkpoint["size"] <= source.stat().st_size:
            with source.open("r+b") as partial:
                partial.truncate(checkpoint["size"])
        checksum = hashlib.sha256()
        with source.open("rb") as partial:
            while block := partial.read(1024 * 1024):
                guard.check()
                checksum.update(block)
        if source.stat().st_size == checkpoint.get("size") and checksum.hexdigest() == checkpoint.get("sha256"):
            validator = checkpoint.get("validator")
            representation = _Representation(checkpoint["responseUrl"], tuple(validator) if validator else None, checkpoint.get("total"))
            retained = True
    if not retained:
        _discard(source)
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            guard.check()
            prefix = source.stat().st_size if source.is_file() else 0
            resume = bool(prefix and representation and representation.validator)
            if not resume:
                prefix = 0
                representation = None
                resumed = False
                _discard(source)
            headers = {"Accept-Encoding": "identity"}
            if resume:
                headers.update({"Range": f"bytes={prefix}-",
                                "If-Range": representation.validator[1]})
            try:
                with handler_factory(guard) as handler, handler.send(Request(url, headers=headers)) as response:
                    guard.check()
                    verify_response(response, response.url)
                    if response.status not in (200, 206):
                        raise HTTPError(response)
                    body_length = _length(response.headers)
                    current_validator = _validator(response.headers)
                    total = body_length
                    append = False
                    if response.status == 206:
                        start, end, total = _range(response.headers)
                        expected_start = prefix if resume else 0
                        segment_length = end - start + 1
                        if (start != expected_start or body_length not in (None, segment_length)
                                or (resume and (response.url != representation.url
                                    or current_validator != representation.validator
                                    or representation.total not in (None, total)))):
                            _discard(source)
                            representation = None
                            resumed = False
                            raise _transfer_error("A origem alterou o arquivo durante a retomada. Vamos tentar novamente.")
                        body_length = segment_length
                        append = resume
                    # If-Range may correctly return 200 for a changed resource or
                    # unsupported Range. Use this complete new response safely.
                    if not append:
                        prefix = 0
                        resumed = False
                        _discard(source)
                    remaining_bytes = guard.maximum_bytes - guard.received
                    if total is not None and total - prefix > remaining_bytes:
                        raise AudioError("A transferência ultrapassa a capacidade configurada.", "source_too_large", 413)
                    if guard.resource_policy and total is not None and total > guard.resource_policy.max_source_bytes:
                        raise AudioError("A origem excede a capacidade configurada.", "source_too_large", 413)
                    guard.check_workspace(max(0, (total or 0) - prefix))
                    representation = _Representation(response.url, current_validator, total)
                    written = 0
                    reporter = getattr(guard, "progress", None)
                    if reporter is not None:
                        reporter.begin_download({"direct": total}, offsets={"direct": prefix}, attempt=attempt)
                        reporter.transfer("direct", prefix, total, attempt=attempt)
                    checksum = hashlib.sha256()
                    if append:
                        with source.open("rb") as previous:
                            while block := previous.read(1024 * 1024):
                                guard.check()
                                checksum.update(block)
                    saved_at = prefix
                    with source.open("ab" if append else "wb") as output:
                        while body_length is None or written < body_length:
                            guard.check()
                            amount = CHUNK_SIZE if body_length is None else min(CHUNK_SIZE, body_length - written)
                            chunk = response.read(amount)
                            guard.check()
                            if not chunk:
                                break
                            if body_length is not None and written + len(chunk) > body_length:
                                _discard(source)
                                representation = None
                                resumed = False
                                raise _transfer_error("A origem retornou um tamanho de arquivo inconsistente.")
                            if guard.resource_policy and prefix + written + len(chunk) > guard.resource_policy.max_source_bytes:
                                raise AudioError("A origem excede a capacidade configurada.", "source_too_large", 413)
                            guard.check_workspace(len(chunk))
                            output.write(chunk)
                            written += len(chunk)
                            checksum.update(chunk)
                            if getattr(guard, "save_partial", None) and representation.validator and prefix + written - saved_at >= 8 * 1024 * 1024:
                                output.flush()
                                os.fsync(output.fileno())
                                guard.save_partial({"kind":"direct", "requestHash":hashlib.sha256(url.encode()).hexdigest(), "responseUrl":response.url, "validator":representation.validator, "total":total, "size":prefix+written, "sha256":checksum.hexdigest()})
                                saved_at = prefix+written
                            resumed = resumed or append
                            if reporter is not None:
                                reporter.transfer("direct", prefix + written, total, attempt=attempt)
                    actual = source.stat().st_size
                    if not actual or (body_length is not None and written != body_length) or (total is not None and actual != total):
                        raise _transfer_error("A transferência foi interrompida antes de concluir o arquivo.", "download_incomplete")
                    if reporter is not None:
                        reporter.transfer("direct", actual, actual, finished=True, attempt=attempt)
                    return source, {"title": title_factory(url), "duration": None, "thumbnail": None,
                                    "source": "Arquivo direto", "webpage_url": url,
                                    "recovery": {"attempts": attempt, "resumed": resumed, "method": "direct_http"}}
            except Exception as exc:
                # Security errors, the aggregate byte limit, cancellation and
                # the original deadline always take precedence over retries.
                if isinstance(exc, HTTPError):
                    exc.response.close()
                if isinstance(exc, AudioError) and exc.code == "invalid_media_response":
                    _discard(source)
                    representation = None
                    resumed = False
                guard.check()
                if attempt >= MAX_ATTEMPTS:
                    exc.recovery_exhausted = True
                    exc.recovery_attempts = attempt
                    raise
                if retry_kind(exc) != "transient" or not wait_for_retry(guard, attempt, exc):
                    raise
        raise AssertionError("bounded transfer loop did not finish")
    except BaseException as exc:
        if not getattr(guard, "save_partial", None) or retry_kind(exc) != "transient":
            _discard(source)
        raise
