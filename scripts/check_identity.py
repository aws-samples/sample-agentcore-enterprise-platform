#!/usr/bin/env python3
"""Verify the identity.mode claim: does the deployed platform's token issuer
match the design, and does a machine token it mints actually pass the
authorizers' checks?

Direct mode has two Entra-side prerequisites that fail SILENTLY at build
time and only bite at first invoke (both hit live while building this):

  1. The app registration has no service principal in the tenant — Cognito
     federation creates one lazily at first sign-in, client_credentials does
     not: `AADSTS7000229 ... missing service principal`.
  2. The app does not request v2 access tokens
     (api.requestedAccessTokenVersion unset): the token then carries the v1
     issuer `https://sts.windows.net/<tenant>/` while the discovery document
     says `.../v2.0`, and every JWT authorizer rejects it on issuer.

So this check mints a token exactly as agents and scripts do and compares its
`iss` and `aud` to what the Gateway and Runtime were told to accept. Brokered
platforms pass trivially (the mode matches and Cognito's shape is fixed).

Usage:
    python scripts/check_identity.py
"""

import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import (
    DEFAULT_ENV,
    DEFAULT_PROJECT,
    auth_mode,
    get_m2m_token,
    get_ssm_param,
    oidc_discovery,
)

PROJECT = os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
ENV = os.environ.get("ENVIRONMENT", DEFAULT_ENV)


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def main() -> int:
    expected_mode = os.environ.get("IDP_MODE", "brokered")
    mode = auth_mode(PROJECT, ENV)
    print(f"Identity mode: deployed={mode} design={expected_mode}")
    if mode != expected_mode:
        print(
            f"FAIL: platform.yaml says identity.mode {expected_mode!r} but the "
            f"deployed auth stack published {mode!r} — run deploy.sh build"
        )
        return 1
    if mode != "direct":
        print("OK: brokered — Cognito issues tokens (nothing IdP-side to check)")
        return 0

    issuer = get_ssm_param("auth/issuer-url", PROJECT, ENV)
    audience = get_ssm_param("auth/app-client-id", PROJECT, ENV)
    advertised = oidc_discovery(issuer)["issuer"]
    try:
        c = claims(get_m2m_token(PROJECT, ENV))
    except Exception as exc:  # noqa: BLE001 — report the IdP's own error text
        msg = str(exc)
        hint = ""
        if "7000229" in msg or "service principal" in msg:
            hint = (
                "\n  → the app registration has no service principal in the tenant: "
                "az ad sp create --id <client-id>"
            )
        print(f"FAIL: could not mint an M2M token: {msg}{hint}")
        return 1

    ok = True
    if c.get("iss") != advertised:
        ok = False
        print(f"FAIL: token iss {c.get('iss')!r} != discovery issuer {advertised!r}")
        if "sts.windows.net" in str(c.get("iss")):
            print(
                "  → the app issues v1 tokens; set api.requestedAccessTokenVersion=2 on "
                "the app registration (az rest PATCH /applications/<object-id>)"
            )
    aud = c.get("aud")
    aud = aud[0] if isinstance(aud, list) and aud else aud
    if aud != audience:
        ok = False
        print(f"FAIL: token aud {aud!r} is not the allowed audience {audience!r}")
    if ok:
        print(f"OK: direct — {advertised} issues tokens for audience {audience}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
