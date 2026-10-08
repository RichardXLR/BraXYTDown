import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import base64
import pytest


@pytest.fixture(autouse=True)
def authenticated_media_test_client(request, monkeypatch):
    """Older media tests operate as a verified user; auth tests stay isolated.

    This substitute exists only inside pytest. Production has no test header,
    environment switch, or anonymous authentication bypass.
    """
    if (request.node.path.name.startswith(("test_auth", "test_account"))
            or request.node.originalname == "test_release_gate_uses_real_api_editing_and_local_converters"):
        return
    from fastapi.testclient import TestClient
    from api import auth
    public = "pk_test_" + base64.b64encode(b"onda-fixture.clerk.accounts.dev$").decode()
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", public)
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_pytest_fixture_not_a_real_key")
    monkeypatch.setenv("CLERK_ALLOWED_ORIGINS", "https://onda-audio.vercel.app")

    async def verified_user(_request, _token, _config):
        return auth.AuthPrincipal("user", user_id="user_fixture", session_id="sess_fixture")

    monkeypatch.setattr(auth, "_verify_session", verified_user)
    original = TestClient.__init__

    def authenticated_client(self, *args, **kwargs):
        kwargs["headers"] = {"Authorization": "Bearer pytest_session", **(kwargs.get("headers") or {})}
        original(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", authenticated_client)
