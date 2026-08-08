"""Conservative scans for stale partial media and confirmed duplicate records."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path


PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


@dataclass(slots=True, frozen=True)
class CleanupCandidate:
    path: str
    size: int
    kind: str
    reason: str


def scan_partial_files(
    destinations: list[Path],
    *,
    active_paths: set[Path] | None = None,
    older_than_hours: int = 24,
    max_files: int = 500,
) -> list[CleanupCandidate]:
    active = {path.resolve() for path in (active_paths or set()) if path.exists()}
    cutoff = time.time() - max(1, older_than_hours) * 3600
    candidates: list[CleanupCandidate] = []
    seen_roots: set[Path] = set()
    for destination in destinations:
        try:
            root = destination.resolve()
        except OSError:
            continue
        if root in seen_roots or not root.is_dir():
            continue
        seen_roots.add(root)
        try:
            iterator = root.rglob("*")
            for path in iterator:
                if len(candidates) >= max_files:
                    return candidates
                try:
                    resolved = path.resolve()
                    if not path.is_file() or resolved in active or not _inside(resolved, root):
                        continue
                    if not path.name.casefold().endswith(PARTIAL_SUFFIXES) or path.stat().st_mtime > cutoff:
                        continue
                    candidates.append(
                        CleanupCandidate(str(path), path.stat().st_size, "partial", "Arquivo parcial sem atividade há mais de 24 horas")
                    )
                except OSError:
                    continue
        except OSError:
            continue
    return candidates


def remove_candidates(candidates: list[CleanupCandidate], destinations: list[Path]) -> tuple[int, int]:
    """Delete only explicit candidates still contained by an approved destination."""

    roots = []
    for item in destinations:
        try:
            roots.append(item.resolve())
        except OSError:
            continue
    removed = bytes_removed = 0
    for candidate in candidates:
        path = Path(candidate.path)
        try:
            resolved = path.resolve()
            if candidate.kind not in {"partial", "duplicate"} or not any(_inside(resolved, root) for root in roots):
                continue
            size = path.stat().st_size
            path.unlink()
            removed += 1
            bytes_removed += size
        except OSError:
            continue
    return removed, bytes_removed


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


__all__ = ["CleanupCandidate", "remove_candidates", "scan_partial_files"]
