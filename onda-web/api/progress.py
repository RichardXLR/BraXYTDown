"""Per-request, bounded progress frames; no global serverless job registry."""
from __future__ import annotations

import json
import math
import queue
import struct
import threading
import time

CONTENT_TYPE = "application/x-onda-download;version=1"
JSON_LIMIT = 16 * 1024
BINARY_LIMIT = 64 * 1024
STAGES = frozenset({"extracting", "downloading", "converting", "delivering", "ready"})
_NUMBERS = {"downloadedBytes", "totalBytes", "speedBytesPerSecond", "processedSeconds",
            "durationSeconds", "outputBytes", "attempt"}


def frame(event: dict) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > JSON_LIMIT:
        raise ValueError("Progress metadata exceeds the frame limit")
    return b"\x01" + struct.pack(">I", len(payload)) + payload


def binary_frame(payload: bytes) -> bytes:
    if not 0 < len(payload) <= BINARY_LIMIT:
        raise ValueError("Invalid download chunk size")
    return b"\x02" + struct.pack(">I", len(payload)) + payload


def accepts_stream(value: str) -> bool:
    for entry in value.lower().split(","):
        parts = [part.strip() for part in entry.split(";")]
        if parts[0] == "application/x-onda-download" and "version=1" in parts[1:]:
            return not any(part in ("q=0", "q=0.0", "q=0.00", "q=0.000") for part in parts[1:])
    return False


class ProgressReporter:
    """Worker threads publish at most four updates/s into eight bounded slots.

    Only explicitly named numeric counters leave the worker. Internal transfer
    keys (filenames/track IDs) remain local and never enter a frame.
    """

    def __init__(self, *, clock=time.monotonic):
        self.events = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.clock = clock
        self.last_emit = -math.inf
        self.stage = None
        self.tracks = {}
        self.expected_tracks = 1
        self.last_transfer_time = clock()
        self.last_transfer_bytes = 0

    def emit(self, stage, *, force=False, **values):
        if stage not in STAGES:
            return
        with self.lock:
            self._emit_locked(stage, force=force, **values)

    def _emit_locked(self, stage, *, force=False, **values):
        now = self.clock()
        if not force and stage == self.stage and now - self.last_emit < .25:
            return
        event = {"type": "progress", "stage": stage}
        for key, value in values.items():
            if (key in _NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 0 <= value <= 2 ** 53 - 1):
                event[key] = value
        self.stage, self.last_emit = stage, now
        # Replacing the oldest *progress* item cannot block a worker or grow
        # memory when the client has a slow connection. File/error/completion
        # frames are written by the owning iterator, never through this queue.
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(event)

    def begin_download(self, tracks=None, *, attempt=1, offsets=None):
        with self.lock:
            self.tracks = {str(key): [(offsets or {}).get(key, 0), total] for key, total in (tracks or {}).items()}
            self.expected_tracks = len(self.tracks) or 1
            self.last_transfer_time = self.clock()
            self.last_transfer_bytes = sum(item[0] for item in self.tracks.values())
            self._emit_locked("downloading", force=True, downloadedBytes=self.last_transfer_bytes, attempt=attempt)

    def transfer(self, key, downloaded, total=None, *, finished=False, attempt=1):
        if not isinstance(downloaded, (int, float)) or isinstance(downloaded, bool) or not math.isfinite(downloaded) or downloaded < 0:
            return
        with self.lock:
            key = str(key)
            previous = self.tracks.get(key, [0, None])
            valid_total = (total if isinstance(total, (int, float)) and not isinstance(total, bool)
                           and math.isfinite(total) and total >= downloaded else
                           previous[1] if previous[1] is not None and previous[1] >= downloaded else None)
            self.tracks[key] = [downloaded, valid_total]
            received = sum(item[0] for item in self.tracks.values())
            now = self.clock()
            elapsed = now - self.last_transfer_time
            values = {"downloadedBytes": received, "attempt": attempt}
            if len(self.tracks) == self.expected_tracks and all(item[1] is not None for item in self.tracks.values()):
                values["totalBytes"] = sum(item[1] for item in self.tracks.values())
            if elapsed >= .25 and received >= self.last_transfer_bytes:
                values["speedBytesPerSecond"] = (received - self.last_transfer_bytes) / elapsed
                self.last_transfer_time, self.last_transfer_bytes = now, received
            self._emit_locked("downloading", force=finished, **values)
