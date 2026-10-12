"""Monotonic wall time of reported stages, including repeated attempts."""
from __future__ import annotations
import math
import threading
import time

STAGES = frozenset({'extracting', 'downloading', 'converting', 'delivering'})


def valid_timings(value):
    return (isinstance(value, dict) and 0 < len(value) <= len(STAGES)
            and not set(value) - STAGES
            and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v < 2 ** 53
                    for v in value.values()))


class StageTiming:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.active = None
        self.since = clock()
        self.elapsed = {}
        self.closed = False

    def enter(self, stage):
        with self.lock:
            if self.closed or stage == self.active:
                return
            now = self.clock()
            if self.active:
                self.elapsed[self.active] += max(0, now - self.since)
            self.active = stage if stage in STAGES else None
            if self.active:
                self.elapsed.setdefault(self.active, 0)
            self.since = now

    def snapshot(self, *, finish=False):
        with self.lock:
            values = dict(self.elapsed)
            if self.active:
                values[self.active] += max(0, self.clock() - self.since)
            if finish:
                self.elapsed = values
                self.active = None
                self.closed = True
            return {key: round(value, 3) for key, value in values.items()}
