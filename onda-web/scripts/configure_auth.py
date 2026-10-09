"""Bind CI-held Clerk keys to the Onda server without opening environment files."""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlencode

from autocura import CureError, HTTP


# Explicit origins for the verified domains of the Onda Vercel project.
# Keep this allowlist in CI so later releases retain custom-domain access.
VERIFIED_APP_ORIGINS = (
    "https://onda-audio.vercel.app",
    "https://ondaittoux.online",
    "https://www.ondaittoux.online",
)


def configure():
    project = os.environ.get("VERCEL_PROJECT_ID", "")
    team = os.environ.get("VERCEL_ORG_ID", "")
    token = os.environ.get("VERCEL_TOKEN", "")
    if not re.fullmatch(r"prj_[A-Za-z0-9]+", project) or not re.fullmatch(r"team_[A-Za-z0-9]+", team) or not token:
        raise CureError("vercel_auth_configuration_missing")
    values = {name: os.environ.get(name, "").strip() for name in (
        "CLERK_SECRET_KEY", "CLERK_PUBLISHABLE_KEY", "CLERK_AUTOCURA_SOURCE_MACHINE_ID", "CLERK_AUTOCURA_TARGET_MACHINE_ID")}
    if (not values["CLERK_SECRET_KEY"].startswith(("sk_test_", "sk_live_"))
            or not values["CLERK_PUBLISHABLE_KEY"].startswith(("pk_test_", "pk_live_"))
            or any(not re.fullmatch(r"mch_[A-Za-z0-9]+", values[name]) for name in (
                "CLERK_AUTOCURA_SOURCE_MACHINE_ID", "CLERK_AUTOCURA_TARGET_MACHINE_ID"))):
        raise CureError("clerk_auth_configuration_missing")
    values["CLERK_ALLOWED_ORIGINS"] = ",".join(VERIFIED_APP_ORIGINS)
    body = [{"key": name, "value": value, "type": "encrypted" if name in (
        "CLERK_SECRET_KEY", "CLERK_PUBLISHABLE_KEY") else "plain",
        "target": ["production", "preview", "development"]} for name, value in values.items()]
    HTTP().json(f"https://api.vercel.com/v10/projects/{project}/env?" + urlencode({"teamId": team, "upsert": "true"}),
                method="POST", payload=body, headers={"Authorization": "Bearer " + token})
    print("Clerk server configuration synchronized securely with Vercel.")


if __name__ == "__main__":
    try:
        configure()
    except CureError as error:
        print("Authentication configuration failed: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
