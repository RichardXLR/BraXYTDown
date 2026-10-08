"""Account settings persisted in private Vercel Blob, never in public assets.

The small HTTP adapter follows @vercel/blob 2.8.1's put/get wire protocol:
https://vercel.com/docs/vercel-blob/using-blob-sdk
https://vercel.com/docs/vercel-blob#conditional-writes
Python's current Blob SDK does not expose conditional writes. Existing
requests is used rather than adding the full Vercel SDK and its dependencies.

register_account expects validated authentication in request.state.onda_auth
with kind='user' and user_id. User IDs and storage paths are never accepted
from the client. BLOB_READ_WRITE_TOKEN stays on the server. Store ID comes
from ONDA_ACCOUNT_BLOB_STORE_ID/BLOB_STORE_ID or the SDK's token convention.
Runtime OIDC is an optional fallback only inside a hosted Vercel environment;
client-supplied OIDC request headers are deliberately ignored.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Literal
import urllib.parse

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
import requests

from .security import AudioError, public_url

MAX_STATE_BYTES = 32 * 1024
MAX_SAFE_INTEGER = 9007199254740991
BLOB_API = "https://vercel.com/api/blob"
BLOB_API_VERSION = "12"
AUDIO_FORMATS = frozenset(("mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"))
VIDEO_FORMATS = frozenset(("mp4", "webm", "mkv", "mov"))
USER_ID = re.compile(r"user_[A-Za-z0-9]{1,120}\Z")
STORE_ID = re.compile(r"[A-Za-z0-9]{1,100}\Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def saved_url(value: str, *, empty=False) -> str:
    if not value and empty:
        return value
    try:
        public_url(value, resolve=False)
    except AudioError as exc:
        raise ValueError("Apenas links públicos HTTP ou HTTPS podem ser salvos.") from exc
    return value


class Preferences(StrictModel):
    accent: Literal["blue", "cyan", "violet"] = "blue"
    theme: Literal["dark", "light", "system"] = "dark"
    density: Literal["comfortable", "compact"] = "comfortable"
    motion: bool = True
    intro: bool = True


class Draft(StrictModel):
    media_type: Literal["audio", "video"] = "audio"
    format: Literal["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff"] = "mp3"
    quality: Literal["128", "192", "256", "320"] = "320"
    video_format: Literal["mp4", "webm", "mkv", "mov"] = "mp4"
    video_resolution: Literal["source", "2160", "1440", "1080", "720", "480", "360"] = "source"
    url: str = Field(default="", max_length=4096)
    trim_start: str = Field(default="", max_length=32)
    trim_end: str = Field(default="", max_length=32)
    strip_metadata: bool = Field(default=True, alias="strip-metadata")
    normalize_audio: bool = Field(default=False, alias="normalize-audio")
    mute_video: bool = Field(default=False, alias="mute-video")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return saved_url(value, empty=True)

    @field_validator("trim_start", "trim_end")
    @classmethod
    def validate_time_text(cls, value):
        # A partially typed draft is legitimate; parsing occurs on download.
        if any(ord(character) < 32 for character in value):
            raise ValueError("Texto de corte inválido.")
        return value


class HistoryOptions(StrictModel):
    video_resolution: Literal["source", "2160", "1440", "1080", "720", "480", "360"] = "source"
    trim_start: float | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER, allow_inf_nan=False)
    trim_end: float | None = Field(default=None, gt=0, le=MAX_SAFE_INTEGER, allow_inf_nan=False)
    strip_metadata: bool = True
    mute: bool = False
    normalize_audio: bool = False

    @model_validator(mode="after")
    def validate_range(self):
        if self.trim_end is not None and self.trim_end <= (self.trim_start or 0):
            raise ValueError("Intervalo de corte inválido.")
        if self.mute and self.normalize_audio:
            raise ValueError("Vídeo silenciado não pode normalizar áudio.")
        return self


class HistoryEntry(StrictModel):
    url: str = Field(min_length=8, max_length=4096)
    media_type: Literal["audio", "video"] = "audio"
    format: Literal["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "aiff", "mp4", "webm", "mkv", "mov"]
    quality: Literal["128", "192", "256", "320", "source"]
    title: str = Field(default="", max_length=400)
    timestamp: int | float = Field(ge=0, le=MAX_SAFE_INTEGER, allow_inf_nan=False)
    options: HistoryOptions = Field(default_factory=HistoryOptions)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return saved_url(value)

    @model_validator(mode="after")
    def validate_media(self):
        allowed = VIDEO_FORMATS if self.media_type == "video" else AUDIO_FORMATS
        if self.format not in allowed:
            raise ValueError("Formato incompatível com o histórico.")
        if self.media_type == "video" and self.quality != "source":
            raise ValueError("Qualidade de vídeo inválida.")
        if self.media_type == "audio" and self.quality == "source" and self.format not in ("wav", "flac", "aiff"):
            raise ValueError("Qualidade de áudio inválida.")
        if self.media_type == "audio" and self.options.mute:
            raise ValueError("Áudio não pode ser silenciado.")
        return self


class AccountState(StrictModel):
    preferences: Preferences = Field(default_factory=Preferences)
    sound: bool = False
    intro_seen: bool = False
    draft: Draft = Field(default_factory=Draft)
    history: list[HistoryEntry] = Field(default_factory=list, max_length=5)


class AccountWrite(StrictModel):
    schema_version: Literal[1] = Field(alias="schema")
    base_revision: int = Field(ge=0, lt=MAX_SAFE_INTEGER)
    state: AccountState

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Versão dos dados inválida.")
        return value


class AccountDocument(StrictModel):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    revision: int = Field(default=0, ge=0, le=MAX_SAFE_INTEGER)
    updated_at: str | None = Field(default=None, max_length=64)
    state: AccountState = Field(default_factory=AccountState)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Versão dos dados inválida.")
        return value

    @field_validator("updated_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("A data deve incluir fuso horário.")
        return value


def unavailable(code="account_storage_unavailable"):
    return AudioError("Não foi possível salvar sua conta agora. Suas alterações continuam neste dispositivo; tente sincronizar novamente.", code, 503)


def conflict():
    return AudioError("Sua conta foi atualizada em outro dispositivo. Recarregue os dados antes de salvar novamente.", "account_revision_conflict", 409)


def encode_document(document: AccountDocument) -> bytes:
    value = document.model_dump(by_alias=True)
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(result) > MAX_STATE_BYTES:
        raise AudioError("Os dados da conta ultrapassam o limite de 32 KB. Reduza os links ou limpe o histórico.", "account_state_too_large", 413)
    return result


def parse_json(raw: bytes):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_json_key")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError("invalid_json_number")

    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)


@dataclass(frozen=True)
class BlobCredentials:
    store_id: str
    token: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        # Never read an .env file; deployment configuration supplies these.
        token = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
        store = os.environ.get("ONDA_ACCOUNT_BLOB_STORE_ID") or os.environ.get("BLOB_STORE_ID", "")
        if token and not store:
            matched = re.fullmatch(r"vercel_blob_rw_([A-Za-z0-9]+)_.+", token)
            store = matched.group(1) if matched else ""
        if not token:
            # No client request header may become a storage credential. The
            # Vercel platform owns runtime OIDC environment rotation.
            if os.environ.get("VERCEL") == "1" and os.environ.get("VERCEL_ENV") in ("production", "preview"):
                token = os.environ.get("VERCEL_OIDC_TOKEN", "").strip()
        store = store.removeprefix("store_")
        if not STORE_ID.fullmatch(store) or not token or len(token) > 16384 or any(character.isspace() for character in token):
            raise unavailable("account_storage_not_configured")
        return cls(store, token)


def account_path(user_id: str) -> str:
    if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
        raise AudioError("Entre na sua conta para acessar os dados salvos.", "authentication_required", 401)
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"accounts/v1/{digest}.json"


def user_identity(request: Request) -> str:
    principal = getattr(request.state, "onda_auth", None)
    kind = principal.get("kind") if isinstance(principal, Mapping) else getattr(principal, "kind", None)
    user_id = principal.get("user_id") if isinstance(principal, Mapping) else getattr(principal, "user_id", None)
    if kind != "user":
        raise AudioError("Entre na sua conta para acessar os dados salvos.", "authentication_required", 401)
    account_path(user_id)
    return user_id


def blob_http(method: str, url: str, *, headers: dict, body: bytes | None = None):
    """Bound network responses and refuse redirects carrying storage credentials."""
    try:
        with requests.request(method, url, headers=headers, data=body, timeout=(3, 8),
                              allow_redirects=False, stream=True) as response:
            declared = response.headers.get("Content-Length")
            if declared and (not declared.isdigit() or int(declared) > MAX_STATE_BYTES):
                raise unavailable()
            chunks, received = [], 0
            for chunk in response.iter_content(chunk_size=4096):
                received += len(chunk)
                if received > MAX_STATE_BYTES:
                    raise unavailable()
                chunks.append(chunk)
            return response.status_code, dict(response.headers), b"".join(chunks)
    except requests.RequestException:
        # Provider messages, request URLs and authorization headers never
        # appear in API errors or logs.
        raise unavailable() from None


def blob_error(status: int, data: bytes, *, conditional=False):
    try:
        parsed = parse_json(data)
        code = parsed.get("error", {}).get("code", "") if isinstance(parsed, dict) else ""
    except (ValueError, TypeError, AttributeError, UnicodeError):
        code = ""
    if conditional and (status in (409, 412) or code in ("precondition_failed", "blob_already_exists", "already_exists")):
        raise conflict()
    if status == 429 or code in ("rate_limited", "store_suspended", "quota_exceeded"):
        raise unavailable("account_storage_busy")
    raise unavailable()


@dataclass(frozen=True)
class StoredAccount:
    document: AccountDocument
    etag: str


class BlobAccountStore:
    def __init__(self, credentials: BlobCredentials):
        self.credentials = credentials

    def read(self, user_id: str) -> StoredAccount | None:
        path = account_path(user_id)
        url = f"https://{self.credentials.store_id}.private.blob.vercel-storage.com/{path}?cache=0"
        # Conditional writes need the validator of the original Blob bytes.
        # CDN compression can change/ weaken the HTTP ETag even though
        # requests transparently decodes the JSON. Keep its representation.
        status, headers, body = blob_http("GET", url, headers={
            "Authorization": f"Bearer {self.credentials.token}", "Accept-Encoding": "identity"})
        if status == 404:
            return None
        if status != 200:
            blob_error(status, body)
        etag = next((value for key, value in headers.items() if key.lower() == "etag"), "")
        if not etag or etag.startswith("W/") or len(etag) > 256 or any(ord(character) < 32 for character in etag):
            raise unavailable()
        try:
            document = AccountDocument.model_validate(parse_json(body))
            encode_document(document)
        except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError, AudioError):
            raise unavailable("account_storage_invalid") from None
        return StoredAccount(document, etag)

    def save(self, user_id: str, update: AccountWrite) -> AccountDocument:
        current = self.read(user_id)
        existing = current.document if current else AccountDocument()
        if existing.revision != update.base_revision:
            raise conflict()
        if current is not None and existing.state == update.state:
            # Focus/retry of an already acknowledged snapshot consumes no PUT.
            return existing
        document = AccountDocument(revision=existing.revision + 1,
                                   updated_at=datetime.now(timezone.utc).isoformat(), state=update.state)
        body = encode_document(document)
        path = account_path(user_id)
        headers = {"Authorization": f"Bearer {self.credentials.token}",
                   "x-vercel-blob-store-id": self.credentials.store_id,
                   "x-api-version": BLOB_API_VERSION, "x-vercel-blob-access": "private",
                   "x-add-random-suffix": "0", "x-allow-overwrite": "1" if current else "0",
                   "x-content-type": "application/json", "Content-Type": "application/json",
                   "x-cache-control-max-age": "60"}
        if current is not None:
            headers["x-if-match"] = current.etag
        url = BLOB_API + "?" + urllib.parse.urlencode({"pathname": path})
        status, _headers, response_body = blob_http("PUT", url, headers=headers, body=body)
        if status not in (200, 201):
            blob_error(status, response_body, conditional=True)
        return document


def account_store():
    return BlobAccountStore(BlobCredentials.from_environment())


def account_response(document: AccountDocument):
    return JSONResponse(document.model_dump(by_alias=True), headers={"Cache-Control": "no-store", "Vary": "Authorization, Cookie"})


def register_account(app):
    @app.get("/api/account/state")
    async def get_account_state(request: Request):
        user_id = user_identity(request)
        store = account_store()
        current = await asyncio.to_thread(store.read, user_id)
        return account_response(current.document if current else AccountDocument())

    @app.put("/api/account/state")
    async def put_account_state(request: Request):
        user_id = user_identity(request)
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_STATE_BYTES:
                raise AudioError("Os dados da conta ultrapassam o limite de 32 KB.", "account_state_too_large", 413)
            chunks.append(chunk)
        try:
            update = AccountWrite.model_validate(parse_json(b"".join(chunks)))
        except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
            raise AudioError("Confira os dados que deseja salvar na conta.", "invalid_account_state", 422) from None
        store = account_store()
        document = await asyncio.to_thread(store.save, user_id, update)
        return account_response(document)
