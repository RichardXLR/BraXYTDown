"""Windows connection-cost and bandwidth schedule helpers."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime

from .command_builder import normalize_rate_limit


_metered_cache = (0.0, False)


def is_metered_connection(cache_seconds: int = 60) -> bool:
    global _metered_cache
    now = time.monotonic()
    if now - _metered_cache[0] < cache_seconds:
        return _metered_cache[1]
    if os.name != "nt":
        _metered_cache = (now, False)
        return False
    script = r"""
$profile = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]::GetInternetConnectionProfile()
if ($null -eq $profile) { 'Unknown' } else { [string]$profile.GetConnectionCost().NetworkCostType }
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = completed.stdout.strip().casefold()
        metered = completed.returncode == 0 and value in {"fixed", "variable"}
    except (OSError, subprocess.SubprocessError):
        metered = False
    _metered_cache = (now, metered)
    return metered


def scheduled_rate_limit(settings: dict, when: datetime | None = None) -> str:
    now = when or datetime.now()
    start = max(0, min(23, int(settings.get("bandwidth_night_start", 22))))
    end = max(0, min(23, int(settings.get("bandwidth_night_end", 7))))
    is_night = now.hour >= start or now.hour < end if start > end else start <= now.hour < end
    key = "bandwidth_night" if is_night else "bandwidth_day"
    return normalize_rate_limit(str(settings.get(key, "")))


__all__ = ["is_metered_connection", "scheduled_rate_limit"]
