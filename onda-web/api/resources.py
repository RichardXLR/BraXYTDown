"""Trusted resource budgets: bytes on disk, transfer work and absolute deadlines."""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import threading

MiB = 1024 ** 2
GiB = 1024 ** 3
_lock = threading.RLock()
_workspaces: set[Path] = set()


def disk_usage(path):
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for name in files:
            item = Path(root, name)
            if not item.is_symlink():
                try:
                    total += item.stat().st_size
                except FileNotFoundError:
                    pass
    return total


def register_workspace(path):
    with _lock:
        _workspaces.add(Path(path).resolve())


def release_workspace(path):
    with _lock:
        _workspaces.discard(Path(path).resolve())


def _integer(env, key, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(env.get(key, default))))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class ResourcePolicy:
    mode: str = "inline"
    max_source_bytes: int = GiB
    max_output_bytes: int = GiB
    max_transfer_bytes: int = 3 * GiB
    max_workspace_bytes: int = 448 * MiB
    reserve_bytes: int = 64 * MiB
    process_seconds: int = 240
    total_seconds: int = 290
    fragment_workers: int = 4

    @classmethod
    def from_environment(cls, env=None):
        env = os.environ if env is None else env
        worker = env.get("ONDA_WORKER_MODE", "").lower() in {"1", "true", "yes"} and not env.get("VERCEL")
        cap = 4 * GiB if worker else GiB
        workspace = 8 * GiB if worker else 448 * MiB
        return cls(mode="worker" if worker else "inline",
                   max_source_bytes=_integer(env, "ONDA_MAX_SOURCE_BYTES", cap, MiB, 64 * GiB),
                   max_output_bytes=_integer(env, "ONDA_MAX_OUTPUT_BYTES", cap, MiB, 64 * GiB),
                   max_transfer_bytes=_integer(env, "ONDA_MAX_TRANSFER_BYTES", cap * 3, MiB, 192 * GiB),
                   max_workspace_bytes=_integer(env, "ONDA_MAX_WORKSPACE_BYTES", workspace, 128 * MiB, 1024 * GiB if worker else workspace),
                   reserve_bytes=_integer(env, "ONDA_RESERVE_BYTES", 64 * MiB, 64 * MiB, GiB),
                   process_seconds=_integer(env, "ONDA_PROCESS_SECONDS", 1800 if worker else 240, 30, 86400 if worker else 240),
                   total_seconds=_integer(env, "ONDA_TOTAL_SECONDS", 1860 if worker else 290, 60, 86500 if worker else 290),
                   fragment_workers=_integer(env, "ONDA_FRAGMENT_WORKERS", 4, 1, 8))

    def workspace_available(self, path):
        from .security import AudioError
        path = Path(path).resolve()
        try:
            with _lock:
                roots = set(_workspaces) | {path}
                # Nested attempt directories are already counted by their task root.
                roots = {p for p in roots if not any(q != p and q in p.parents for q in roots)}
                used = sum(disk_usage(p) for p in roots)
                free = shutil.disk_usage(path).free - self.reserve_bytes
            return min(self.max_workspace_bytes - used, free)
        except OSError as exc:
            raise AudioError("O armazenamento temporário está indisponível.", "storage_busy", 503) from exc

    def check_workspace(self, path, extra_bytes=0):
        from .security import AudioError
        available = self.workspace_available(path)
        if available < extra_bytes:
            raise AudioError("Não há espaço temporário suficiente para concluir esta tarefa. Tente novamente ou use o worker de arquivos grandes.", "workspace_exhausted", 413)
        return available

    def check_output(self, size):
        from .security import AudioError
        if not 0 < size <= self.max_output_bytes:
            raise AudioError("O arquivo está vazio ou ultrapassa a capacidade configurada.", "output_too_large", 413)

    def capabilities(self):
        return {"mode": self.mode, "maxSourceBytes": self.max_source_bytes,
                "maxOutputBytes": self.max_output_bytes, "maxTransferBytes": self.max_transfer_bytes,
                "maxWorkspaceBytes": self.max_workspace_bytes, "reserveBytes": self.reserve_bytes,
                "workTimeoutSeconds": self.process_seconds, "totalTimeoutSeconds": self.total_seconds,
                "scope": "instance", "sizeDependsOnWorkspace": True}
