"""CI authentication configuration stays scoped to Onda's verified domains."""
import base64
import importlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from api import auth


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("configure_auth")
    environment = {
        "VERCEL_PROJECT_ID": "prj_fixture",
        "VERCEL_ORG_ID": "team_fixture",
        "VERCEL_TOKEN": "fixture_vercel_not_a_real_token",
        "CLERK_SECRET_KEY": "sk_test_fixture_only_not_a_real_key",
        "CLERK_PUBLISHABLE_KEY": "pk_test_" + base64.b64encode(
            b"onda-fixture.clerk.accounts.dev$"
        ).decode(),
        "CLERK_AUTOCURA_SOURCE_MACHINE_ID": "mch_sourcefixture",
        "CLERK_AUTOCURA_TARGET_MACHINE_ID": "mch_targetfixture",
        "CLERK_ALLOWED_ORIGINS": "https://untrusted.example",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    # Preview-only derived origins must not influence this configuration test.
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("VERCEL_URL", raising=False)
    calls = []

    class FakeHTTP:
        def json(self, url, **kwargs):
            calls.append((url, kwargs))
            return {}

    monkeypatch.setattr(module, "HTTP", FakeHTTP)
    return module, environment, calls


def test_ci_upsert_keeps_both_custom_domains_and_canonical_domain(setup, monkeypatch, capsys):
    module, environment, calls = setup
    module.configure()
    assert len(calls) == 1
    url, options = calls[0]
    endpoint = urlparse(url)
    assert endpoint.scheme == "https" and endpoint.netloc == "api.vercel.com"
    assert endpoint.path == "/v10/projects/prj_fixture/env"
    assert parse_qs(endpoint.query) == {"teamId": ["team_fixture"], "upsert": ["true"]}
    assert options["method"] == "POST"
    assert options["headers"] == {"Authorization": "Bearer " + environment["VERCEL_TOKEN"]}
    values = {entry["key"]: entry for entry in options["payload"]}
    assert all(entry["target"] == ["production", "preview", "development"]
               for entry in values.values())
    assert values["CLERK_SECRET_KEY"]["type"] == "encrypted"
    assert values["CLERK_PUBLISHABLE_KEY"]["type"] == "encrypted"
    assert values["CLERK_ALLOWED_ORIGINS"]["type"] == "plain"

    # Exercise the actual server parser with the environment CI will publish.
    monkeypatch.setenv("CLERK_ALLOWED_ORIGINS", values["CLERK_ALLOWED_ORIGINS"]["value"])
    assert set(auth.configuration().allowed_origins) == {
        "https://onda-audio.vercel.app",
        "https://ondaittoux.online",
        "https://www.ondaittoux.online",
    }
    output = capsys.readouterr()
    assert not output.err
    assert environment["VERCEL_TOKEN"] not in output.out
    assert environment["CLERK_SECRET_KEY"] not in output.out


@pytest.mark.parametrize("name,value,error", [
    ("VERCEL_PROJECT_ID", "other-project", "vercel_auth_configuration_missing"),
    ("VERCEL_ORG_ID", "other-team", "vercel_auth_configuration_missing"),
    ("VERCEL_TOKEN", "", "vercel_auth_configuration_missing"),
    ("CLERK_SECRET_KEY", "", "clerk_auth_configuration_missing"),
    ("CLERK_PUBLISHABLE_KEY", "invalid", "clerk_auth_configuration_missing"),
    ("CLERK_AUTOCURA_TARGET_MACHINE_ID", "invalid", "clerk_auth_configuration_missing"),
])
def test_missing_ci_configuration_never_mutates_environment(setup, monkeypatch, name, value, error):
    module, _, calls = setup
    monkeypatch.setenv(name, value)
    with pytest.raises(module.CureError, match=error):
        module.configure()
    assert calls == []
