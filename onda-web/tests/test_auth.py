"""Authentication boundary tests; no live Clerk users or credentials required."""
import asyncio
import base64
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from api import auth


FAPI = "onda-auth-test.clerk.accounts.dev"
PUBLISHABLE_KEY = "pk_test_" + base64.b64encode((FAPI + "$").encode()).decode()
SECRET_KEY = "sk_test_fixture_only_not_a_real_key"
ORIGIN = "https://onda-audio.vercel.app"


@pytest.fixture
def configured(monkeypatch):
    for name in ("CLERK_PUBLISHABLE_KEY", "CLERK_SECRET_KEY", "CLERK_ALLOWED_ORIGINS",
                 "CLERK_AUTOCURA_SOURCE_MACHINE_ID", "CLERK_AUTOCURA_TARGET_MACHINE_ID",
                 "CLERK_JWT_KEY", "VERCEL_URL", "AUTOCURA_AUDIO_CANARY_URL",
                 "AUTOCURA_VIDEO_CANARY_URL", "AUTOCURA_YOUTUBE_AUDIO_CANARY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", PUBLISHABLE_KEY)
    monkeypatch.setenv("CLERK_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("CLERK_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("CLERK_AUTOCURA_SOURCE_MACHINE_ID", "mch_autocura")
    monkeypatch.setenv("CLERK_AUTOCURA_TARGET_MACHINE_ID", "mch_ondaapi")
    return auth.configuration()


@pytest.fixture
def claims(configured):
    now = int(time.time())
    return {"iss": configured.issuer, "azp": ORIGIN, "sub": "user_fixture",
            "sid": "sess_fixture", "iat": now, "nbf": now - 5, "exp": now + 60,
            "sts": "active"}


@pytest.fixture
def test_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "auth": auth.auth_capabilities()}

    @app.api_route("/api/account", methods=["GET", "PUT"])
    async def account(request: Request):
        principal = request.state.onda_auth
        return {"kind": principal.kind, "user_id": principal.user_id}

    @app.api_route("/api/download", methods=["POST", "GET"])
    async def download(request: Request):
        body = await request.json() if request.method == "POST" else None
        return {"kind": request.state.onda_auth.kind, "body": body}

    @app.post("/api/compatibility/test")
    async def compatibility_test(request: Request):
        return {"kind": request.state.onda_auth.kind, "body": await request.json()}

    @app.get("/api/maintenance")
    async def maintenance(request: Request):
        return {"kind": request.state.onda_auth.kind}

    @app.get("/asset.js")
    async def asset():
        return {"asset": True}

    auth.register_auth(app)
    return app


def mock_session_client(monkeypatch, claims, *, signed_in=True):
    calls = []

    class Client:
        async def authenticate_request_async(self, request, options):
            calls.append((request, options))
            return SimpleNamespace(is_signed_in=signed_in, payload=claims)

    monkeypatch.setattr(auth, "_clerk_client", lambda secret_key: Client())
    return calls


def mock_machine_client(monkeypatch, **updates):
    result = {"subject": "mch_autocura", "scopes": ["mch_ondaapi"], "revoked": False,
              "expired": False, "expiration": time.time() * 1000 + 60000}
    result.update(updates)
    calls = []

    class M2M:
        async def verify_token_async(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(**result)

    monkeypatch.setattr(auth, "_clerk_client", lambda secret_key: SimpleNamespace(m2m=M2M()))
    return calls


def test_missing_configuration_fails_closed_but_login_shell_and_health_remain_available(test_app, configured, monkeypatch):
    monkeypatch.delenv("CLERK_SECRET_KEY")
    with TestClient(test_app) as client:
        assert client.get("/api/health").json()["auth"]["configured"] is False
        config = client.get("/api/auth/config").json()
        assert config == {"enabled": True, "configured": False, "publishableKey": None,
                          "frontendApi": None, "code": "auth_not_configured"}
        response = client.post("/api/download", json={"url": ORIGIN + "/canary.wav"})
        assert response.status_code == 503
        assert response.json()["code"] == "auth_not_configured"
        assert client.get("/asset.js").status_code == 200


def test_config_response_never_contains_secret_or_machine_configuration(test_app, configured):
    with TestClient(test_app) as client:
        response = client.get("/api/auth/config")
        assert response.json() == {"enabled": True, "configured": True,
            "publishableKey": PUBLISHABLE_KEY, "frontendApi": "https://" + FAPI,
            "code": "ready"}
        assert SECRET_KEY not in response.text
        assert "mch_" not in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "bad"},
    {"Authorization": "Basic token"}, {"Authorization": "Bearer "},
    {"Authorization": "Bearer malformed token"}, {"Cookie": "__session=pretend.token.value"}])
def test_every_api_request_requires_bearer_not_ui_or_cookie_only_auth(test_app, configured, headers):
    with TestClient(test_app) as client:
        response = client.get("/api/account", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "auth_required"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["www-authenticate"] == "Bearer"


def test_verified_identity_is_attached_and_session_only_option_is_used(test_app, configured, claims, monkeypatch):
    calls = mock_session_client(monkeypatch, claims)
    with TestClient(test_app) as client:
        response = client.get("/api/account", headers={"Authorization": "bearer fixture.token.value",
                              "Origin": ORIGIN, "Cookie": "__session_wrong=another-token"})
        assert response.status_code == 200
        assert response.json() == {"kind": "user", "user_id": "user_fixture"}
    assert calls[0][1].accepts_token == ["session_token"]
    assert calls[0][1].authorized_parties == [ORIGIN]
    assert calls[0][0].headers["Authorization"] == "Bearer fixture.token.value"
    assert "cookie" not in calls[0][0].headers


@pytest.mark.parametrize("update", [
    {"iss": "https://different-instance.clerk.accounts.dev"}, {"iss": None},
    {"azp": "https://attacker.example"}, {"azp": None}, {"sub": "user_a/../b"},
    {"sub": "mch_not_user"}, {"sub": None}, {"sid": "session_wrong"}, {"sid": None},
    {"exp": 1}, {"exp": None}, {"exp": True}, {"iat": None}, {"nbf": None},
    {"iat": 4102444800}, {"nbf": 4102444800}, {"iat": -1}, {"sts": "pending"},
])
def test_verified_but_wrong_or_incomplete_session_claims_are_rejected(test_app, configured, claims, monkeypatch, update):
    claims.update(update)
    mock_session_client(monkeypatch, claims)
    with TestClient(test_app) as client:
        response = client.get("/api/account", headers={"Authorization": "Bearer fixture.token.value"})
        assert response.status_code == 401


def test_unapproved_request_origin_cannot_extend_allowed_parties(test_app, configured, claims, monkeypatch):
    calls = mock_session_client(monkeypatch, claims)
    with TestClient(test_app) as client:
        response = client.get("/api/account", headers={"Authorization": "Bearer fixture.token.value",
                              "Origin": "https://attacker.example"})
        assert response.status_code == 401
    assert calls == []


def test_unsigned_sdk_result_is_never_accepted(test_app, configured, claims, monkeypatch):
    mock_session_client(monkeypatch, claims, signed_in=False)
    with TestClient(test_app) as client:
        assert client.get("/api/account", headers={"Authorization": "Bearer fixture.token.value"}).status_code == 401


@pytest.mark.parametrize("variable,value", [
    ("CLERK_PUBLISHABLE_KEY", "pk_test_invalid"), ("CLERK_SECRET_KEY", "missing-prefix"),
    ("CLERK_ALLOWED_ORIGINS", "*"), ("CLERK_ALLOWED_ORIGINS", "https://evil.example/path"),
    ("CLERK_ALLOWED_ORIGINS", "https://user:password@example.com"),
    ("CLERK_ALLOWED_ORIGINS", "https://*.example.com"), ("CLERK_ALLOWED_ORIGINS", "http://remote.example"),
    ("CLERK_ALLOWED_ORIGINS", "[]"), ("CLERK_ALLOWED_ORIGINS", '["https://onda-audio.vercel.app", 5]'),
])
def test_invalid_administrator_configuration_never_opens_api(test_app, configured, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with TestClient(test_app) as client:
        assert client.get("/api/account", headers={"Authorization": "Bearer fixture.token.value"}).status_code == 503


def test_json_origin_configuration_accepts_local_dev_without_wildcards(configured, monkeypatch):
    monkeypatch.setenv("CLERK_ALLOWED_ORIGINS", json.dumps([ORIGIN + "/", "http://localhost:8011"]))
    assert auth.configuration().allowed_origins == (ORIGIN, "http://localhost:8011")


def test_machine_runs_owned_canary_through_same_endpoint_and_body(test_app, configured, monkeypatch):
    calls = mock_machine_client(monkeypatch)
    body = {"url": ORIGIN + "/canary.mp4", "media_type": "video", "format": "mp4"}
    with TestClient(test_app) as client:
        response = client.post("/api/download", json=body, headers={"Authorization": "Bearer mt_fixture"})
        assert response.status_code == 200
        assert response.json() == {"kind": "machine", "body": body}
        assert client.get("/api/maintenance", headers={"Authorization": "Bearer mt_fixture"}).status_code == 200
    assert calls[0] == {"token": "mt_fixture", "retries": None, "timeout_ms": 10000}


@pytest.mark.parametrize("body", [
    {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
    {"url": "https://attacker.example/canary.wav"},
    {"url": ORIGIN + "/private.wav"}, {"url": ORIGIN + "/canary.wav?secret=1"},
    {"url": ORIGIN + "/canary.wav#fragment"},
    {"url": ORIGIN + "/canary.wav", "cookies": "sensitive user cookie"},
    {"url": ORIGIN + "/canary.wav", "cookies": {"secret": "cookie"}},
])
def test_machine_cannot_download_user_media_or_cookies(test_app, configured, monkeypatch, body):
    mock_machine_client(monkeypatch)
    with TestClient(test_app) as client:
        response = client.post("/api/download", json=body, headers={"Authorization": "Bearer mt_fixture"})
        assert response.status_code == 403
        assert response.json()["code"] == "auth_forbidden"


def test_machine_can_check_fixed_youtube_metadata_but_not_account_or_download_it(test_app, configured, monkeypatch):
    mock_machine_client(monkeypatch)
    headers = {"Authorization": "Bearer mt_fixture"}
    with TestClient(test_app) as client:
        assert client.post("/api/compatibility/test", json={"url": next(iter(auth.YOUTUBE_METADATA_CANARIES))}, headers=headers).status_code == 200
        assert client.post("/api/compatibility/test", json={"url": "https://www.youtube.com/watch?v=notacanary"}, headers=headers).status_code == 403
        assert client.get("/api/account", headers=headers).status_code == 403
        assert client.put("/api/account", json={}, headers=headers).status_code == 403
        assert client.get("/api/download", headers=headers).status_code == 403
        assert client.post("/api/auth/config", headers=headers).status_code == 403


@pytest.mark.parametrize("update", [
    {"subject": "mch_someoneelse"}, {"scopes": []}, {"scopes": None},
    {"revoked": True}, {"expired": True}, {"expiration": 1}, {"expiration": None},
    {"expiration": float("inf")}, {"expiration": True},
])
def test_wrong_expired_or_revoked_machine_token_is_rejected(test_app, configured, monkeypatch, update):
    mock_machine_client(monkeypatch, **update)
    with TestClient(test_app) as client:
        assert client.post("/api/download", json={"url": ORIGIN + "/canary.wav"},
                           headers={"Authorization": "Bearer mt_fixture"}).status_code == 401


def test_machine_requires_explicit_ids_and_blocks_large_payload(test_app, configured, monkeypatch):
    mock_machine_client(monkeypatch)
    headers = {"Authorization": "Bearer mt_fixture"}
    with TestClient(test_app) as client:
        response = client.post("/api/download", json={"url": ORIGIN + "/canary.wav", "padding": "x" * 17000}, headers=headers)
        assert response.status_code == 403
        monkeypatch.delenv("CLERK_AUTOCURA_SOURCE_MACHINE_ID")
        assert client.post("/api/download", json={"url": ORIGIN + "/canary.wav"}, headers=headers).status_code == 401


def machine_request(chunks, headers=()):
    """ASGI chunks with no assumed HTTP Content-Length or request buffering."""
    messages = iter(chunks)
    received = []

    async def receive():
        chunk = next(messages)
        received.append(chunk)
        return {"type": "http.request", "body": chunk,
                "more_body": len(received) < len(chunks)}

    request = Request({"type": "http", "method": "POST", "path": "/api/download",
                       "query_string": b"", "scheme": "https", "headers": list(headers),
                       "server": ("onda-audio.vercel.app", 443)}, receive=receive)
    return request, received


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"2")]])
def test_oversized_machine_stream_stops_before_buffering_the_remaining_body(configured, headers):
    request, received = machine_request(
        [b"x" * 8192, b"x" * 8192, b"x", b"must-not-be-consumed"], headers)
    with pytest.raises(auth.AuthFailure) as failure:
        asyncio.run(auth._authorize_machine(request, configured))
    assert failure.value.status == 403
    assert len(received) == 3


def test_allowed_chunked_machine_body_is_replayed_unchanged_to_the_route(configured):
    raw = json.dumps({"url": ORIGIN + "/canary.wav", "media_type": "audio"}).encode()
    request, received = machine_request([raw[:8], raw[8:]])

    async def authorize_and_read():
        await auth._authorize_machine(request, configured)
        assert await request.body() == raw
        assert await request.json() == json.loads(raw)

    asyncio.run(authorize_and_read())
    assert len(received) == 2


@pytest.mark.parametrize("body", [
    b'{"url":"' + ORIGIN.encode() + b'/canary.wav","url":"' + ORIGIN.encode() + b'/canary.wav"}',
    b'{"url":"' + ORIGIN.encode() + b'/canary.wav","cookies":null,"cookies":""}',
    b'{"url":"' + ORIGIN.encode() + b'/canary.wav","padding":NaN}',
    b'{"url":"' + ORIGIN.encode() + b'/canary.wav","padding":Infinity}',
    b'{"url":"' + ORIGIN.encode() + b'/canary.wav","padding":-Infinity}',
    b"[" * 2000 + b"0" + b"]" * 2000,
    b"\xff",
])
def test_ambiguous_or_malformed_machine_json_is_forbidden_without_server_error(
        test_app, configured, monkeypatch, body):
    mock_machine_client(monkeypatch)
    with TestClient(test_app) as client:
        response = client.post("/api/download", content=body,
                               headers={"Authorization": "Bearer mt_fixture"})
    assert response.status_code == 403
    assert response.json()["code"] == "auth_forbidden"


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"1"), (b"content-length", b"1")],
    [(b"content-length", b"9" * 5000)],
    [(b"content-length", b"-1")],
])
def test_ambiguous_or_unbounded_machine_length_is_rejected_without_reading(configured, headers):
    request, received = machine_request([b"must-not-be-consumed"], headers)
    with pytest.raises(auth.AuthFailure) as failure:
        asyncio.run(auth._authorize_machine(request, configured))
    assert failure.value.status == 403
    assert received == []


def test_sdk_error_body_with_secret_is_not_returned(test_app, configured, monkeypatch):
    class Client:
        async def authenticate_request_async(self, *_args):
            raise RuntimeError("upstream request leaked " + SECRET_KEY)
    monkeypatch.setattr(auth, "_clerk_client", lambda secret: Client())
    with TestClient(test_app) as client:
        response = client.get("/api/account", headers={"Authorization": "Bearer fixture.token.value"})
        assert response.status_code == 401
        assert SECRET_KEY not in response.text


@pytest.fixture
def signing_keys(monkeypatch):
    pytest.importorskip("clerk_backend_api")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setenv("CLERK_JWT_KEY", public.decode())
    return private


@pytest.mark.parametrize("scenario", ["valid", "forged", "expired", "wrong_origin", "wrong_issuer", "missing_exp"])
def test_real_official_sdk_verifies_rsa_session_tokens(test_app, configured, claims, signing_keys, scenario):
    import jwt
    if scenario == "expired": claims["exp"] = int(time.time()) - 60
    if scenario == "wrong_origin": claims["azp"] = "https://attacker.example"
    if scenario == "wrong_issuer": claims["iss"] = "https://other.clerk.accounts.dev"
    if scenario == "missing_exp": claims.pop("exp")
    token = jwt.encode(claims, signing_keys, algorithm="RS256", headers={"kid": "test-key"})
    if scenario == "forged":
        prefix, payload, signature = token.split(".")
        forged = dict(claims, sub="user_attacker")
        payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        token = ".".join((prefix, payload, signature))
    with TestClient(test_app) as client:
        response = client.get("/api/account", headers={"Authorization": "Bearer " + token})
        assert response.status_code == (200 if scenario == "valid" else 401)
        if scenario == "valid": assert response.json()["user_id"] == claims["sub"]
