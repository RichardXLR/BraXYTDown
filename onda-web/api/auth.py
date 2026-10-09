"""Clerk session verification and narrowly scoped AutoCura authentication.

The application's API never trusts a client-supplied user identifier.  Session
tokens are verified by Clerk's official SDK before a principal reaches a route.
AutoCura uses a separate Clerk machine identity, not an anonymous bypass.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from functools import lru_cache
import json
import math
import os
import re
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse


CANONICAL_ORIGIN = "https://onda-audio.vercel.app"
YOUTUBE_METADATA_CANARIES = frozenset({
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
})
MACHINE_READ_PATHS = frozenset({
    "/api/health", "/api/compatibility", "/api/compatibility/providers",
    "/api/maintenance",
})
MACHINE_MEDIA_PATHS = frozenset({
    "/api/download", "/api/inspect", "/api/player", "/api/compatibility/test",
})
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/config"})
MAX_MACHINE_BODY = 16 * 1024


@dataclass(frozen=True)
class AuthPrincipal:
    kind: Literal["user", "machine"]
    user_id: str | None = None
    session_id: str | None = None
    machine_id: str | None = None


@dataclass(frozen=True)
class AuthConfiguration:
    publishable_key: str
    frontend_api: str
    issuer: str
    secret_key: str = field(repr=False)
    allowed_origins: tuple[str, ...] = ()
    source_machine_id: str | None = None
    target_machine_id: str | None = None
    jwt_key: str | None = field(default=None, repr=False)


class AuthFailure(Exception):
    def __init__(self, code="auth_required", status=401):
        super().__init__(code)
        self.code = code
        self.status = status


def _origin(value: str) -> str | None:
    """Accept configured origins, never paths, userinfo or wildcard origins."""
    try:
        parsed = urlsplit(value)
        if (parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or not parsed.hostname):
            return None
        hostname = parsed.hostname
        if not re.fullmatch(r"[A-Za-z0-9.-]+", hostname) or "*" in hostname:
            return None
        local = hostname in {"localhost", "127.0.0.1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            return None
        port = parsed.port
        suffix = f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else ""
        return f"{parsed.scheme}://{hostname.lower()}{suffix}"
    except (ValueError, TypeError):
        return None


def _frontend_api(publishable_key: str) -> str | None:
    if not re.fullmatch(r"pk_(test|live)_[A-Za-z0-9_+/=-]+", publishable_key):
        return None
    try:
        encoded = publishable_key.split("_", 2)[2]
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                  altchars=b"-_", validate=True).decode("ascii")
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not decoded.endswith("$"):
        return None
    hostname = decoded[:-1]
    if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname)
            or "." not in hostname or ".." in hostname):
        return None
    return "https://" + hostname


def configuration() -> AuthConfiguration:
    """Read process configuration only; never open or emit environment files."""
    publishable_key = os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip()
    frontend_api = _frontend_api(publishable_key)
    secret_key = os.environ.get("CLERK_SECRET_KEY", "").strip()
    raw_origins = os.environ.get("CLERK_ALLOWED_ORIGINS", "").strip()
    try:
        values = json.loads(raw_origins) if raw_origins.startswith("[") else raw_origins.split(",")
        if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
            raise ValueError("invalid origins")
        origins = tuple(dict.fromkeys(_origin(value.strip()) for value in values))
        if None in origins or not all(origins):
            raise ValueError("invalid origins")
    except (ValueError, TypeError, json.JSONDecodeError):
        raise AuthFailure("auth_not_configured", 503) from None
    if (not frontend_api or not secret_key.startswith(("sk_test_", "sk_live_"))
            or len(secret_key) < 16 or len(secret_key) > 512):
        raise AuthFailure("auth_not_configured", 503)

    # The deployment's own hostname is supplied by Vercel, never by Origin
    # or Host headers. This allows a candidate to be tested before promotion.
    deployment_hostname = os.environ.get("VERCEL_URL", "").strip()
    if (os.environ.get("VERCEL") == "1" and os.environ.get("VERCEL_ENV") == "preview"
            and re.fullmatch(r"[a-z0-9-]+\.vercel\.app", deployment_hostname)):
        origins = tuple(dict.fromkeys((*origins, "https://" + deployment_hostname)))

    def machine_id(name):
        value = os.environ.get(name, "").strip()
        return value if re.fullmatch(r"mch_[A-Za-z0-9]+", value) else None

    return AuthConfiguration(
        publishable_key=publishable_key, frontend_api=frontend_api,
        issuer=frontend_api, secret_key=secret_key, allowed_origins=origins,
        source_machine_id=machine_id("CLERK_AUTOCURA_SOURCE_MACHINE_ID"),
        target_machine_id=machine_id("CLERK_AUTOCURA_TARGET_MACHINE_ID"),
        jwt_key=os.environ.get("CLERK_JWT_KEY") or None,
    )


def auth_capabilities() -> dict:
    try:
        config = configuration()
        configured = True
        machine = bool(config.source_machine_id and config.target_machine_id)
    except AuthFailure:
        configured = machine = False
    return {"required": True, "configured": configured, "provider": "clerk",
            "machineToMachine": machine}


@lru_cache(maxsize=1)
def _clerk_client(secret_key):
    try:
        from clerk_backend_api import Clerk
    except ImportError:
        raise AuthFailure("auth_unavailable", 503) from None
    return Clerk(bearer_auth=secret_key, timeout_ms=10000, retry_config=None)


def _bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1 or len(values[0]) > 16384:
        raise AuthFailure()
    found = re.fullmatch(r"Bearer ([A-Za-z0-9_.-]+)", values[0], flags=re.IGNORECASE)
    if not found:
        raise AuthFailure()
    return found[1]


def _validate_session(payload, config: AuthConfiguration) -> AuthPrincipal:
    if not isinstance(payload, dict):
        raise AuthFailure()
    now = time.time()
    timestamps = [payload.get(name) for name in ("exp", "iat", "nbf")]
    if (any(type(value) is not int for value in timestamps)
            or timestamps[0] <= now or timestamps[1] > now + 5 or timestamps[2] > now + 5
            or timestamps[0] <= max(timestamps[1], timestamps[2])
            or min(timestamps) < 0 or payload.get("sts", "active") != "active"
            or payload.get("iss") != config.issuer
            or payload.get("azp") not in config.allowed_origins
            or not isinstance(payload.get("sub"), str)
            or not re.fullmatch(r"user_[A-Za-z0-9]+", payload["sub"])
            or not isinstance(payload.get("sid"), str)
            or not re.fullmatch(r"sess_[A-Za-z0-9]+", payload["sid"])):
        raise AuthFailure()
    return AuthPrincipal("user", user_id=payload["sub"], session_id=payload["sid"])


async def _verify_session(request: Request, token: str, config: AuthConfiguration) -> AuthPrincipal:
    try:
        from clerk_backend_api.security.types import AuthenticateRequestOptions
        # Passing only our normalized bearer header avoids Clerk's permissive
        # cookie-prefix matching and keeps state-changing requests CSRF-safe.
        clerk_request = httpx.Request(request.method, str(request.url),
                                      headers={"Authorization": "Bearer " + token})
        options = AuthenticateRequestOptions(
            accepts_token=["session_token"], authorized_parties=list(config.allowed_origins),
            jwt_key=config.jwt_key, clock_skew_in_ms=5000,
        )
        state = await asyncio.wait_for(
            _clerk_client(config.secret_key).authenticate_request_async(clerk_request, options),
            timeout=15,
        )
    except AuthFailure:
        raise
    except (ImportError, asyncio.TimeoutError, httpx.HTTPError):
        raise AuthFailure("auth_unavailable", 503) from None
    except Exception:
        # SDK errors can include credential-bearing HTTP details. Do not expose
        # them in a response or log them through an unhandled exception.
        raise AuthFailure("auth_required", 401) from None
    if not state.is_signed_in:
        raise AuthFailure()
    return _validate_session(state.payload, config)


async def _verify_machine(token: str, config: AuthConfiguration) -> AuthPrincipal:
    if not config.source_machine_id or not config.target_machine_id:
        raise AuthFailure()
    try:
        result = await asyncio.wait_for(
            _clerk_client(config.secret_key).m2m.verify_token_async(
                token=token, retries=None, timeout_ms=10000), timeout=15)
    except AuthFailure:
        raise
    except (asyncio.TimeoutError, httpx.HTTPError):
        raise AuthFailure("auth_unavailable", 503) from None
    except Exception:
        raise AuthFailure() from None
    expiration = getattr(result, "expiration", None)
    scopes = getattr(result, "scopes", None)
    subject = getattr(result, "subject", None)
    if (subject != config.source_machine_id
            or not isinstance(scopes, list) or config.target_machine_id not in scopes
            or getattr(result, "revoked", None) is not False
            or getattr(result, "expired", None) is not False
            or type(expiration) not in {int, float} or not math.isfinite(expiration)
            or expiration <= time.time() * 1000):
        raise AuthFailure()
    return AuthPrincipal("machine", machine_id=subject)


def _machine_canaries(config: AuthConfiguration) -> tuple[set[str], set[str]]:
    origins = {CANONICAL_ORIGIN}
    origins.update(origin for origin in config.allowed_origins if origin.startswith("https://"))
    deployment_hostname = os.environ.get("VERCEL_URL", "").strip()
    if re.fullmatch(r"[a-z0-9-]+\.vercel\.app", deployment_hostname):
        origins.add("https://" + deployment_hostname)
    owned = {origin + path for origin in origins
             for path in ("/canary.wav", "/canary.mp4", "/canary.ts")}
    for variable in ("AUTOCURA_AUDIO_CANARY_URL", "AUTOCURA_VIDEO_CANARY_URL"):
        value = os.environ.get(variable, "").strip()
        # These are administrator-owned explicit fixture URLs, not credentials.
        if value and _safe_canary(value):
            owned.add(value)
    audio_youtube = os.environ.get("AUTOCURA_YOUTUBE_AUDIO_CANARY_URL", "").strip()
    if audio_youtube in YOUTUBE_METADATA_CANARIES:
        owned.add(audio_youtube)
    metadata = owned | set(YOUTUBE_METADATA_CANARIES)
    return owned, metadata


def _safe_canary(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


async def _authorize_machine(request: Request, config: AuthConfiguration):
    path = request.url.path
    if request.method in {"GET", "HEAD"} and path in MACHINE_READ_PATHS:
        return
    if request.method != "POST" or path not in MACHINE_MEDIA_PATHS:
        raise AuthFailure("auth_forbidden", 403)
    length = request.headers.get("content-length")
    if length and (not length.isascii() or not length.isdigit() or int(length) > MAX_MACHINE_BODY):
        raise AuthFailure("auth_forbidden", 403)
    try:
        raw = await request.body()
        if len(raw) > MAX_MACHINE_BODY:
            raise ValueError("large body")
        body = json.loads(raw)
        if (not isinstance(body, dict) or not isinstance(body.get("url"), str)
                or any(body.get(field) not in {None, ""}
                       for field in ("cookies", "video_password", "user_agent"))):
            raise ValueError("invalid canary")
    except (ValueError, TypeError):
        raise AuthFailure("auth_forbidden", 403) from None
    owned, metadata = _machine_canaries(config)
    allowed = metadata if path == "/api/compatibility/test" else owned
    if body["url"].strip() not in allowed:
        raise AuthFailure("auth_forbidden", 403)


async def authenticate_request(request: Request) -> AuthPrincipal:
    config = configuration()
    token = _bearer(request)
    origin = request.headers.get("origin")
    if origin is not None and _origin(origin) not in config.allowed_origins:
        raise AuthFailure()
    if token.startswith(("mt_", "m2m_")):
        principal = await _verify_machine(token, config)
        await _authorize_machine(request, config)
        return principal
    return await _verify_session(request, token, config)


def register_auth(app):
    @app.get("/api/auth/config")
    async def auth_config():
        try:
            config = configuration()
        except AuthFailure:
            return {"enabled": True, "configured": False, "publishableKey": None,
                    "frontendApi": None, "code": "auth_not_configured"}
        return {"enabled": True, "configured": True,
                "publishableKey": config.publishable_key,
                "frontendApi": config.frontend_api, "code": "ready"}

    @app.middleware("http")
    async def require_account(request: Request, call_next):
        path = request.url.path
        if ((path == "/api" or path.startswith("/api/"))
                and not (request.method in {"GET", "HEAD"} and path in PUBLIC_PATHS)):
            try:
                request.state.onda_auth = await authenticate_request(request)
            except AuthFailure as exc:
                message = ("Entre na sua conta para usar o Onda." if exc.status == 401 else
                           "Esta conta não tem acesso a esta operação." if exc.status == 403 else
                           "O login está indisponível. Tente novamente em alguns instantes.")
                return JSONResponse({"error": message, "code": exc.code},
                    status_code=exc.status, headers={"Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff", "WWW-Authenticate": "Bearer"})
        return await call_next(request)
