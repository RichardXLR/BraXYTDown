"""User isolation and lost-update prevention with mocked private Blob transport."""
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import urllib.parse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
import requests

from api import account
from api.security import AudioError


class FakeBlob:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.counter = 0
        self.before_write = None
        self.failure = None

    def request(self, method, url, *, headers, body=None):
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.parse_qs(parsed.query)["pathname"][0] if method == "PUT" else parsed.path.lstrip("/")
        self.calls.append((method, url, headers, body))
        if self.failure:
            return self.failure
        if method == "GET":
            assert parsed.hostname == "exampleaccountstore.private.blob.vercel-storage.com"
            assert urllib.parse.parse_qs(parsed.query) == {"cache": ["0"]}
            if path not in self.files:
                return 404, {}, b""
            data, etag = self.files[path]
            return 200, {"eTag": etag}, data
        assert method == "PUT"
        assert parsed.hostname == "vercel.com" and parsed.path == "/api/blob"
        assert headers["x-vercel-blob-access"] == "private"
        assert headers["x-add-random-suffix"] == "0"
        assert headers["x-api-version"] == "12"
        assert headers["x-vercel-blob-store-id"] == "exampleaccountstore"
        if self.before_write:
            callback, self.before_write = self.before_write, None
            callback(path)
        existing = self.files.get(path)
        if existing and headers["x-allow-overwrite"] == "0":
            return 409, {}, b'{"error":{"code":"already_exists"}}'
        if headers.get("x-if-match") and (not existing or headers["x-if-match"] != existing[1]):
            return 412, {}, b'{"error":{"code":"precondition_failed"}}'
        self.counter += 1
        self.files[path] = body, f'"etag-{self.counter}"'
        return 201, {}, b'{"pathname":"redacted-account-path"}'


@pytest.fixture
def state():
    return account.AccountState().model_dump(by_alias=True)


@pytest.fixture
def client(monkeypatch):
    fake = FakeBlob()
    monkeypatch.setattr(account, "blob_http", fake.request)
    monkeypatch.setattr(account, "account_store", lambda: account.BlobAccountStore(
        account.BlobCredentials("exampleaccountstore", "test-credential-not-for-output")))
    app = FastAPI()

    @app.middleware("http")
    async def verified_test_principal(request, call_next):
        # A substitute for cryptographic auth, confined to this test app.
        principal = request.headers.get("x-test-user")
        if principal:
            request.state.onda_auth = {"kind": "user", "user_id": principal}
        elif request.headers.get("x-test-service"):
            request.state.onda_auth = {"kind": "service"}
        return await call_next(request)

    @app.exception_handler(AudioError)
    async def known_error(_request, exc):
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)

    account.register_account(app)
    with TestClient(app) as browser:
        browser.blob = fake
        yield browser


def put(client, state, revision=0, user="user_Alice"):
    return client.put("/api/account/state", headers={"x-test-user": user},
                      json={"schema": 1, "base_revision": revision, "state": state})


def test_account_defaults_match_frontend(client, state):
    response = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert response.status_code == 200
    assert response.json() == {"schema": 1, "revision": 0, "updated_at": None, "state": state}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization, Cookie"
    assert "user_Alice" not in client.blob.calls[0][1]


@pytest.mark.parametrize("headers", [{}, {"x-test-service": "1"}, {"x-test-user": "../../user_Alice"}])
def test_anonymous_service_or_invalid_identity_cannot_read_or_write(client, state, headers):
    for method in ("get", "put"):
        response = getattr(client, method)("/api/account/state", headers=headers,
                                           **({"json": {"schema": 1, "base_revision": 0, "state": state}} if method == "put" else {}))
        assert response.status_code == 401
    assert client.blob.calls == []


def test_settings_are_persistent_and_isolated_by_verified_user(client, state):
    state["preferences"]["accent"] = "cyan"
    state["sound"] = True
    state["draft"]["url"] = "https://vimeo.com/12345"
    created = put(client, state)
    assert created.status_code == 200
    assert created.json()["revision"] == 1
    assert created.json()["updated_at"].endswith("+00:00")
    saved = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert saved.json() == created.json()
    other = client.get("/api/account/state", headers={"x-test-user": "user_Bob"})
    assert other.json()["revision"] == 0
    assert other.json()["state"]["draft"]["url"] == ""
    assert len(client.blob.files) == 1
    assert account.account_path("user_Alice") != account.account_path("user_Bob")


def test_conflicting_client_revision_does_not_overwrite(client, state):
    assert put(client, state).status_code == 200
    state["sound"] = True
    conflicting = put(client, state)
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "account_revision_conflict"
    assert len([call for call in client.blob.calls if call[0] == "PUT"]) == 1
    assert not json.loads(next(iter(client.blob.files.values()))[0])["state"]["sound"]


def test_etag_prevents_write_after_race_during_save(client, state):
    assert put(client, state).status_code == 200
    original = copy.deepcopy(client.blob.files)

    def concurrent_write(path):
        stored = json.loads(original[path][0])
        stored["revision"] = 2
        stored["state"]["preferences"]["theme"] = "light"
        client.blob.files[path] = json.dumps(stored).encode(), '"another-device"'

    client.blob.before_write = concurrent_write
    state["sound"] = True
    response = put(client, state, revision=1)
    assert response.status_code == 409
    last_headers = client.blob.calls[-1][2]
    assert last_headers["x-if-match"] == '"etag-1"'
    assert last_headers["x-allow-overwrite"] == "1"
    stored = json.loads(next(iter(client.blob.files.values()))[0])
    assert stored["state"]["preferences"]["theme"] == "light"
    assert not stored["state"]["sound"]


def test_first_save_race_is_atomic_create(client, state):
    def other_first_write(path):
        document = account.AccountDocument(revision=1).model_dump(by_alias=True)
        document["state"]["sound"] = True
        client.blob.files[path] = json.dumps(document).encode(), '"other-first-write"'

    client.blob.before_write = other_first_write
    response = put(client, state)
    assert response.status_code == 409
    assert client.blob.calls[-1][2]["x-allow-overwrite"] == "0"
    assert "x-if-match" not in client.blob.calls[-1][2]
    assert json.loads(next(iter(client.blob.files.values()))[0])["state"]["sound"]


def test_unchanged_acknowledged_snapshot_consumes_no_write(client, state):
    saved = put(client, state).json()
    repeated = put(client, state, revision=1)
    assert repeated.json() == saved
    assert len([call for call in client.blob.calls if call[0] == "PUT"]) == 1


def test_real_account_transport_cancels_conflict_body_before_reload_and_preserves_session_cancellation():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is unavailable for the browser transport regression test.')
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([node, str(root / 'tests/account_transport.cjs')],
                               cwd=root, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'Account transport: 409 cancellation/retry' in completed.stdout


def test_actual_account_lifecycle_finishes_sdk_logout_without_reopening_the_session():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is unavailable for the browser lifecycle regression test.')
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([node, str(root / 'tests/account_lifecycle.cjs')],
                               cwd=root, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'Account lifecycle: pending SDK logout' in completed.stdout


@pytest.mark.parametrize("key,value", [("cookies", "secret"), ("user_id", "user_Bob"), ("session", "secret")])
def test_unknown_sensitive_fields_are_rejected_before_storage(client, state, key, value):
    state[key] = value
    response = put(client, state)
    assert response.status_code == 422
    assert value not in response.text
    assert client.blob.calls == []


@pytest.mark.parametrize("change", [
    lambda state: state["preferences"].update({"accent": "red"}),
    lambda state: state.update({"sound": 1}),
    lambda state: state["draft"].update({"url": "https://localhost/private"}),
    lambda state: state["draft"].update({"url": "https://user:password@example.com"}),
    lambda state: state["draft"].update({"url": "https://example.com/" + "a" * 4096}),
    lambda state: state["draft"].update({"trim_start": "a" * 33}),
    lambda state: state["draft"].update({"cookies": "do-not-save"}),
])
def test_invalid_preferences_drafts_and_urls_do_not_write(client, state, change):
    change(state)
    assert put(client, state).status_code == 422
    assert client.blob.calls == []


def history_entry(**overrides):
    entry = {"url": "https://example.com/song.wav", "media_type": "audio", "format": "wav",
             "quality": "source", "title": "Áudio longo", "timestamp": 1730000000000,
             "options": {"trim_start": 20000, "trim_end": 30000, "strip_metadata": True}}
    return entry | overrides


def test_long_media_options_and_unicode_history_are_saved(client, state):
    state["history"] = [history_entry(), history_entry(media_type="video", format="mp4", title="Vídeo 📽️")]
    response = put(client, state)
    assert response.status_code == 200
    assert response.json()["state"]["history"][0]["options"]["trim_end"] == 30000
    assert response.json()["state"]["history"][1]["title"] == "Vídeo 📽️"


@pytest.mark.parametrize("entry", [
    history_entry(format="mp4"), history_entry(quality="320", media_type="video", format="mp4"),
    history_entry(timestamp=True), history_entry(timestamp=float("inf")),
    history_entry(title="a" * 401), history_entry(cookies="never-save"),
    history_entry(options={"trim_start": 3, "trim_end": 2}),
    history_entry(options={"mute": True}),
    history_entry(media_type="video", format="mp4", options={"mute": True, "normalize_audio": True}),
])
def test_invalid_history_entries_are_rejected(client, state, entry):
    state["history"] = [entry]
    payload = json.dumps({"schema": 1, "base_revision": 0, "state": state})
    response = client.put("/api/account/state", headers={"x-test-user": "user_Alice"}, content=payload)
    assert response.status_code == 422
    assert client.blob.calls == []


def test_history_count_is_bounded(client, state):
    state["history"] = [history_entry()] * 6
    assert put(client, state).status_code == 422
    assert client.blob.calls == []


@pytest.mark.parametrize("body", [
    b'{"schema":1,"schema":1,"base_revision":0,"state":{}}',
    b'{"schema":true,"base_revision":0,"state":{}}',
    b'{"schema":1,"base_revision":0,"state":{"sound":NaN}}',
    b'{"schema":1,"base_revision":0,"state":{"history":[]},"user_id":"user_Bob"}',
    b'[]', b'null', b'not json',
])
def test_ambiguous_or_invalid_envelopes_are_rejected(client, body):
    response = client.put("/api/account/state", headers={"x-test-user": "user_Alice"}, content=body)
    assert response.status_code == 422
    assert client.blob.calls == []


def test_input_body_limit_is_bytes_not_unicode_characters(client, state):
    state["history"] = [history_entry(title="📽" * 400, url="https://example.com/" + "a" * 4000)] * 5
    state["draft"]["url"] = "https://example.com/" + "b" * 4000
    raw = json.dumps({"schema": 1, "base_revision": 0, "state": state}, ensure_ascii=False).encode()
    assert len(raw) > account.MAX_STATE_BYTES
    response = client.put("/api/account/state", headers={"x-test-user": "user_Alice"}, content=raw)
    assert response.status_code == 413
    assert response.json()["code"] == "account_state_too_large"
    assert client.blob.calls == []


@pytest.mark.parametrize("failure", [
    (429, {}, b'{"error":{"code":"rate_limited","message":"provider-token-secret"}}'),
    (403, {}, b'{"error":{"code":"store_suspended","message":"provider-token-secret"}}'),
    (500, {}, b'provider-token-secret'), (302, {"Location": "https://evil.example"}, b''),
])
def test_storage_failures_are_retryable_and_never_expose_provider_details(client, failure):
    client.blob.failure = failure
    response = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert response.status_code == 503
    assert response.json()["code"].startswith("account_storage_")
    assert "provider-token-secret" not in response.text
    assert "evil.example" not in response.text


def test_invalid_stored_state_is_not_silently_reset_or_overwritten(client, state):
    path = account.account_path("user_Alice")
    client.blob.files[path] = b'{"schema":1,"revision":2,"state":{"cookies":"secret"}}', '"etag"'
    response = put(client, state, revision=2)
    assert response.status_code == 503
    assert response.json()["code"] == "account_storage_invalid"
    assert "secret" not in response.text
    assert [call[0] for call in client.blob.calls] == ["GET"]


@pytest.mark.parametrize("missing", ["schema", "revision", "updated_at", "state", "all"])
def test_truncated_stored_envelope_never_becomes_a_new_empty_account(client, state, missing):
    document = account.AccountDocument(revision=1).model_dump(by_alias=True)
    if missing == "all":
        document = {}
    else:
        del document[missing]
    path = account.account_path("user_Alice")
    original = json.dumps(document).encode()
    client.blob.files[path] = original, '"original-stored-version"'
    loaded = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert loaded.status_code == 503
    assert loaded.json()["code"] == "account_storage_invalid"
    # Both the old and default revision are rejected before an overwrite,
    # protecting data even if a client retries with an empty local snapshot.
    for revision in (0, 1):
        failed_save = put(client, state, revision=revision)
        assert failed_save.status_code == 503
        assert failed_save.json()["code"] == "account_storage_invalid"
    assert all(call[0] == "GET" for call in client.blob.calls)
    assert client.blob.files[path] == (original, '"original-stored-version"')


@pytest.mark.parametrize("section", ["preferences", "sound", "intro_seen", "draft", "history"])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_incomplete_stored_state_sections_never_reset_saved_account(client, state, section, damage):
    document = account.AccountDocument(revision=1).model_dump(by_alias=True)
    if damage == "missing":
        del document["state"][section]
    else:
        # An empty JSON object is not a complete preference/draft model and
        # cannot be substituted for bool/list sections either.
        document["state"][section] = {}
    path = account.account_path("user_Alice")
    original = json.dumps(document).encode()
    client.blob.files[path] = original, '"original-stored-version"'
    loaded = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert loaded.status_code == 503
    assert loaded.json()["code"] == "account_storage_invalid"
    saved = put(client, state, revision=1)
    assert saved.status_code == 503
    assert saved.json()["code"] == "account_storage_invalid"
    assert all(call[0] == "GET" for call in client.blob.calls)
    assert client.blob.files[path] == (original, '"original-stored-version"')


STORED_SECTION_FIELDS = (
    [("preferences", field.alias or name) for name, field in account.Preferences.model_fields.items()]
    + [("draft", field.alias or name) for name, field in account.Draft.model_fields.items()]
    + [("history-entry", field.alias or name) for name, field in account.HistoryEntry.model_fields.items()]
    + [("history-options", field.alias or name) for name, field in account.HistoryOptions.model_fields.items()]
)


@pytest.mark.parametrize("section,missing", STORED_SECTION_FIELDS)
def test_partial_nested_stored_models_cannot_reintroduce_defaults(client, state, section, missing):
    populated = account.AccountState(history=[account.HistoryEntry.model_validate(history_entry())])
    document = account.AccountDocument(revision=1, state=populated).model_dump(by_alias=True)
    nested = (document["state"][section] if section in {"preferences", "draft"}
              else document["state"]["history"][0])
    if section == "history-options":
        nested = nested["options"]
    del nested[missing]
    path = account.account_path("user_Alice")
    original = json.dumps(document).encode()
    client.blob.files[path] = original, '"original-stored-version"'
    response = put(client, state, revision=1)
    assert response.status_code == 503
    assert response.json()["code"] == "account_storage_invalid"
    assert [call[0] for call in client.blob.calls] == ["GET"]
    assert client.blob.files[path] == (original, '"original-stored-version"')


def test_empty_stored_state_is_rejected_while_nonexistent_account_keeps_defaults(client):
    missing = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert missing.status_code == 200
    assert missing.json()["revision"] == 0
    document = account.AccountDocument(revision=1).model_dump(by_alias=True)
    document["state"] = {}
    client.blob.files[account.account_path("user_Alice")] = json.dumps(document).encode(), '"etag"'
    response = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert response.status_code == 503
    assert response.json()["code"] == "account_storage_invalid"


def test_complete_existing_schema_snapshot_with_empty_history_stays_compatible(client, state):
    document = account.AccountDocument(revision=2).model_dump(by_alias=True)
    document["state"]["preferences"]["accent"] = "cyan"
    document["state"]["draft"]["url"] = "https://vimeo.com/12345"
    client.blob.files[account.account_path("user_Alice")] = json.dumps(document).encode(), '"etag"'
    response = client.get("/api/account/state", headers={"x-test-user": "user_Alice"})
    assert response.status_code == 200
    assert response.json() == document
    assert response.json()["state"]["history"] == []


def test_static_blob_credentials_are_derived_and_redacted(monkeypatch):
    monkeypatch.delenv("ONDA_ACCOUNT_BLOB_STORE_ID", raising=False)
    monkeypatch.delenv("BLOB_STORE_ID", raising=False)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_teststore_placeholdercredential")
    credentials = account.BlobCredentials.from_environment()
    assert credentials.store_id == "teststore"
    assert "placeholdercredential" not in repr(credentials)


def test_explicit_store_configuration_is_normalized(monkeypatch):
    monkeypatch.setenv("ONDA_ACCOUNT_BLOB_STORE_ID", "store_teststore")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "placeholdercredential")
    assert account.BlobCredentials.from_environment().store_id == "teststore"


@pytest.mark.parametrize("configuration", [
    {"VERCEL": "", "VERCEL_ENV": "", "VERCEL_OIDC_TOKEN": "placeholder"},
    {"VERCEL": "1", "VERCEL_ENV": "development", "VERCEL_OIDC_TOKEN": "placeholder"},
    {"VERCEL": "1", "VERCEL_ENV": "production", "VERCEL_OIDC_TOKEN": ""},
])
def test_oidc_is_not_accepted_outside_hosted_supported_environments(monkeypatch, configuration):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("BLOB_STORE_ID", "store_teststore")
    monkeypatch.delenv("ONDA_ACCOUNT_BLOB_STORE_ID", raising=False)
    for name, value in configuration.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(AudioError) as error:
        account.BlobCredentials.from_environment()
    assert error.value.status == 503
    assert error.value.code == "account_storage_not_configured"


def test_hosted_runtime_oidc_fallback_does_not_require_read_write_token(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("ONDA_ACCOUNT_BLOB_STORE_ID", raising=False)
    monkeypatch.setenv("BLOB_STORE_ID", "store_teststore")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "placeholderruntimecredential")
    credentials = account.BlobCredentials.from_environment()
    assert credentials.store_id == "teststore"
    assert "placeholderruntimecredential" not in repr(credentials)


def test_dataclass_principals_are_supported():
    @dataclass
    class Principal:
        kind: str
        user_id: str

    class State:
        onda_auth = Principal("user", "user_Alice")

    class FakeRequest:
        state = State()

    assert account.user_identity(FakeRequest()) == "user_Alice"


def test_http_transport_refuses_redirects_and_bounds_stream(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def iter_content(self, chunk_size):
            assert chunk_size == 4096
            yield b"a" * (account.MAX_STATE_BYTES + 1)

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(account.requests, "request", request)
    with pytest.raises(AudioError) as error:
        account.blob_http("GET", "https://teststore.private.blob.vercel-storage.com/state.json",
                          headers={"Authorization": "Bearer placeholder"})
    assert error.value.status == 503
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == (3, 8)


def test_http_transport_network_error_is_redacted(monkeypatch):
    def request(*_args, **_kwargs):
        raise requests.RequestException("provider-token-secret")

    monkeypatch.setattr(account.requests, "request", request)
    with pytest.raises(AudioError) as error:
        account.blob_http("PUT", account.BLOB_API, headers={"Authorization": "Bearer placeholder"})
    assert error.value.status == 503
    assert "provider-token-secret" not in str(error.value)


def test_conditional_write_uses_the_original_blob_representation(monkeypatch):
    original = account.AccountDocument(revision=1)
    raw = account.encode_document(original)
    etag = '"original-blob-version"'
    def negotiated_blob(method, url, *, headers, body=None):
        if method == "GET":
            validator = etag if headers.get("Accept-Encoding") == "identity" else 'W/"compressed-blob-version"'
            return 200, {"ETag": validator}, raw
        if headers.get("x-if-match") != etag:
            return 412, {}, b'{"error":{"code":"precondition_failed"}}'
        return 200, {}, b'{}'
    monkeypatch.setattr(account, "blob_http", negotiated_blob)
    store = account.BlobAccountStore(account.BlobCredentials("teststore", "placeholder"))
    state = original.state.model_copy(update={"sound": True})
    result = store.save("user_Synthetic", account.AccountWrite(schema=1, base_revision=1, state=state))
    assert result.revision == 2 and result.state.sound is True


def test_weak_blob_validator_is_not_used_for_conditional_overwrites(monkeypatch):
    raw = account.encode_document(account.AccountDocument(revision=1))
    monkeypatch.setattr(account, "blob_http", lambda *args, **kwargs: (200, {"etag": 'W/"weak-version"'}, raw))
    store = account.BlobAccountStore(account.BlobCredentials("teststore", "placeholder"))
    with pytest.raises(AudioError) as failure:
        store.read("user_Synthetic")
    assert failure.value.status == 503


@pytest.mark.parametrize("resolution", ["1440", "2160"])
def test_high_resolution_selection_survives_account_roundtrip(client, state, resolution):
    state["draft"].update(media_type="video", video_resolution=resolution)
    state["history"] = [{"url": "https://sample.example.com/4k.mp4", "media_type": "video", "format": "mp4", "quality": "source", "timestamp": 1, "options": {"video_resolution": resolution}}]
    response = put(client, state)
    assert response.status_code == 200
    restored = client.get("/api/account/state", headers={"x-test-user": "user_Alice"}).json()["state"]
    assert restored["draft"]["video_resolution"] == resolution
    assert restored["history"][0]["options"]["video_resolution"] == resolution
