"""Bounded instance-local admission, metadata cache and safe diagnostics."""
from __future__ import annotations
from contextlib import contextmanager
import copy
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from urllib.parse import urlsplit
from .security import AudioError

_secret = os.urandom(32)
_lock = threading.RLock()
_active = Counter()
_keys = OrderedDict()
_cache = OrderedDict()
_metrics = Counter()
_circuits = OrderedDict()


def digest(value):
    return hmac.new(_secret, value.encode(), hashlib.sha256).hexdigest()


def principal(request):
    actor = getattr(getattr(request, "state", None), "onda_auth", None)
    return digest(str(getattr(actor, "user_id", None) or getattr(actor, "machine_id", None) or "unknown"))


def admit(request, body):
    owner = principal(request)
    key = getattr(request, "headers", {}).get("idempotency-key")
    if key is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise AudioError("A identificação da tarefa é inválida.", "invalid_request", 422)
    fingerprint = digest(body.model_dump_json())
    now = time.monotonic()
    with _lock:
        for item in list(_keys):
            if _keys[item][1] < now:
                del _keys[item]
        identity = (owner, digest(key)) if key else None
        if identity in _keys:
            code = "idempotency_conflict" if _keys[identity][0] != fingerprint else "duplicate_download"
            raise AudioError("Esta tarefa já foi enviada. Aguarde o resultado ou inicie uma nova tarefa.", code, 409)
        if _active[owner] >= 1:
            raise AudioError("Sua conta já está processando uma mídia. Aguarde ou cancele a tarefa atual.", "user_busy", 429)
        if identity:
            _keys[identity] = (fingerprint, now + 600)
            while len(_keys) > 2048:
                _keys.popitem(last=False)
        _active[owner] += 1
        _metrics["started"] += 1
    closed = False

    def release():
        nonlocal closed
        with _lock:
            if not closed:
                closed = True
                _active[owner] -= 1
                if not _active[owner]:
                    del _active[owner]
                _metrics["released"] += 1
    return owner, release


def metadata_key(body):
    # Credential-bearing and signed links must never enter a shared cache.
    if body.cookies or body.video_password or body.user_agent:
        return None
    query = urlsplit(body.url).query.lower()
    if any(word in query for word in ("token", "sign", "auth", "key", "expire", "password")):
        return None
    return digest(body.url + "|" + body.media_type)


def cached_metadata(key, result=None):
    if key is None:
        return None
    with _lock:
        now = time.monotonic()
        if result is not None:
            encoded = json.dumps(result).encode()
            if len(encoded) <= 64 * 1024:
                _cache[key] = (now + 60, copy.deepcopy(result))
                _cache.move_to_end(key)
                while len(_cache) > 128:
                    _cache.popitem(last=False)
            return None
        value = _cache.get(key)
        if value and value[0] > now:
            _cache.move_to_end(key)
            _metrics["metadataCacheHits"] += 1
            return copy.deepcopy(value[1])
        _cache.pop(key, None)
        return None


@contextmanager
def provider_circuit(provider, partition="public"):
    from .recovery import retry_kind, _NEVER_CODES, _status
    key = digest(provider + "|" + partition)
    now = time.monotonic()
    with _lock:
        failures, until, trial = _circuits.get(key, (0, 0, False))
        if until > now or trial:
            raise AudioError("A origem está temporariamente indisponível. Aguarde antes de tentar novamente.", "upstream_unavailable", 503)
        if until:
            _circuits[key] = (failures, 0, True)
        while len(_circuits) > 512:
            _circuits.popitem(last=False)
    try:
        yield
    except Exception as exc:
        with _lock:
            code = getattr(exc, "code", None)
            transient = code not in _NEVER_CODES and (code in {"upstream_timeout", "upstream_unavailable", "upstream_rate_limited", "dns_failed", "rate_limited"} or _status(exc) in {408, 429, 500, 502, 503, 504} or retry_kind(exc) == "transient")
            if transient:
                failures += 1
                _circuits[key] = (failures, time.monotonic() + 30 if failures >= 3 else 0, False)
            else:
                _circuits.pop(key, None)
        raise
    else:
        with _lock:
            _circuits.pop(key, None)


def diagnostics():
    with _lock:
        return {"scope": "instance", "activeDownloads": sum(_active.values()),
                "metadataCacheEntries": len(_cache), **dict(_metrics)}
