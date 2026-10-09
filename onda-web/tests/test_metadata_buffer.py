"""Exercise resource bounds against fragmented and oversized upstream bodies."""
import io
import tracemalloc

import pytest
from yt_dlp.networking import Response

from api import security
from api.security import AudioError, BoundedResponse, Guard


class TinyChunks(io.BytesIO):
    def read(self, amount=-1):
        return super().read(min(amount, 16))


def test_fragmented_metadata_keeps_memory_bounded_and_preserves_every_byte():
    payload = bytes(range(256)) * 256
    guard = Guard()
    response = BoundedResponse(Response(TinyChunks(payload), 'https://example.org/metadata', {}), guard)
    # The input buffer predates tracing; measure only the receiving algorithm.
    tracemalloc.start()
    try:
        received = response.read()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert received == payload
    assert guard.received == len(payload)
    assert peak < 3 * len(payload) + 16 * 1024


@pytest.mark.parametrize('amount', [None, -1])
def test_oversized_metadata_reads_only_one_extra_byte_and_aborts_peers(monkeypatch, amount):
    monkeypatch.setattr(security, 'MAX_METADATA_BYTES', 1024)
    guard = Guard()
    response = BoundedResponse(Response(io.BytesIO(b'x' * 4096), 'https://example.org/metadata', {}), guard)
    with pytest.raises(AudioError) as failed:
        response.read(amount)
    assert failed.value.code == 'metadata_too_large'
    assert guard.received == 1025
    assert guard.cancelled.is_set()
    with pytest.raises(AudioError) as following:
        guard.check()
    assert following.value is failed.value


@pytest.mark.parametrize('size', [0, 1023, 1024])
def test_metadata_accepts_empty_and_exact_limit_bodies(monkeypatch, size):
    monkeypatch.setattr(security, 'MAX_METADATA_BYTES', 1024)
    guard = Guard()
    response = BoundedResponse(Response(io.BytesIO(b'x' * size), 'https://example.org/metadata', {}), guard)
    assert response.read() == b'x' * size
    assert guard.received == size
    assert not guard.cancelled.is_set()
