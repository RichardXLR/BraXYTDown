"""Bounded HTTP transfer recovery through the existing public-only transport.

Only an unchanged representation may be resumed. All attempts share the
original Guard: neither elapsed time nor bytes already received are reset.
"""
from __future__ import annotations

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
    etag = headers.get("ETag", "").strip()
    if len(etag) <= 512 and _STRONG_ETAG.fullmatch(etag):
        return "ETag", etag
    modified = headers.get("Last-Modified", "").strip()
    if not modified or len(modified) > 128 or "\r" in modified or "\n" in modified:
        return None
    try:
        date = parsedate_to_datetime(modified)
        if date.tzinfo is not None and date.utcoffset().total_seconds() == 0:
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
                        raise AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)
                    representation = _Representation(response.url, current_validator, total)
                    written = 0
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
                            output.write(chunk)
                            written += len(chunk)
                            resumed = resumed or append
                    actual = source.stat().st_size
                    if not actual or (body_length is not None and written != body_length) or (total is not None and actual != total):
                        raise _transfer_error("A transferência foi interrompida antes de concluir o arquivo.", "download_incomplete")
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
    except BaseException:
        _discard(source)
        raise
