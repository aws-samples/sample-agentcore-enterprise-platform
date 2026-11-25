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
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import (
    DEFAULT_ENV,
    DEFAULT_PROJECT,
    auth_mode,
    get_m2m_token,
    get_ssm_param,
    oidc_discovery,
)

from infra_utils.platform_config import load_platform_config

REPO = Path(__file__).resolve().parents[1]
PROJECT = os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
ENV = os.environ.get("ENVIRONMENT", DEFAULT_ENV)


def configured_audience(environ: dict | None = None) -> str:
    """The Okta audience the authorizers were told to pin: IDP_AUDIENCE when
    exported, else identity.audience from the validated platform.yaml (the
    same file app.py synthesized from)."""
    environ = os.environ if environ is None else environ
    if environ.get("IDP_AUDIENCE"):
        return environ["IDP_AUDIENCE"]
    path = Path(environ.get("PLATFORM_CONFIG", "platform.yaml"))
    if not path.is_absolute() and not path.exists():
        path = REPO / path
    if not path.exists():
        return ""
    return load_platform_config(str(path)).identity.audience


def okta_mint_hint(msg: str) -> str:
    """The Okta-side fix for a failed client_credentials request."""
    if "invalid_scope" in msg:
        return (
            "\n  → define the custom scope (identity.m2m_scope) on the authorization "
            "server (Security > API > Authorization Servers > Scopes) and allow it "
            "in an Access Policy rule for this client"
        )
    if "unauthorized_client" in msg:
        return (
            "\n  → enable the Client Credentials grant on the Okta app (an API "
            "Services app, or the web app with that grant enabled)"
        )
    if "invalid_client" in msg:
        return (
            "\n  → the client secret in Secrets Manager is wrong, expired, or has "
            "trailing whitespace"
        )
    return ""


# RFC 6749 section 5.2 error codes.
OAUTH_ERRORS = (
    "invalid_request",
    "invalid_client",
    "invalid_grant",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
)

# What is printed for each code: built from the constants above, looked up by the code.
OAUTH_TEXT = {c: f", OAuth error: {c}" for c in OAUTH_ERRORS}


def oauth_error_code(exc: BaseException) -> str:
    """The OAuth `error` code (RFC 6749 5.2) from a token endpoint's
    HTTPError body, or "". Only that field is read: str(HTTPError) carries just
    the HTTP status, and the free-text error_description is never printed."""
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    try:
        body = json.loads(exc.read() or b"{}")
    except (ValueError, OSError, AttributeError):
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    # Return our own constant, never the response's string: what is printed can then hold
    # nothing the token request carried.
    return next((known for known in OAUTH_ERRORS if known == code), "")


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

    idp = os.environ.get("IDP_TYPE", "entra_id")
    issuer = get_ssm_param("auth/issuer-url", PROJECT, ENV)
    if idp == "okta":
        # Okta tokens carry the authorization server's audience, not a client id.
        audience = configured_audience()
        if not audience:
            print(
                "FAIL: idp okta needs identity.audience in platform.yaml "
                "(or IDP_AUDIENCE) to check aud"
            )
            return 1
    else:
        audience = get_ssm_param("auth/app-client-id", PROJECT, ENV)
    advertised = oidc_discovery(issuer)["issuer"]
    try:
        c = claims(get_m2m_token(PROJECT, ENV))
    except Exception as exc:  # noqa: BLE001 — report the IdP's own error text
        msg = str(exc)
        code = oauth_error_code(exc)
        hint = ""
        if idp == "okta":
            hint = okta_mint_hint(code or msg)
        elif "7000229" in msg or "service principal" in msg:
            hint = (
                "\n  → the app registration has no service principal in the tenant: "
                "az ad sp create --id <client-id>"
            )
        # Only fixed words and the OAuth error code are printed: the raw text of a
        # failed token request can carry the client credentials (CodeQL
        # py/clear-text-logging-sensitive-data). The hint above already names the fix.
        kind = (
            "HTTP error"
            if isinstance(exc, urllib.error.HTTPError)
            else "request failed"
        )
        print(
            f"FAIL: could not mint an M2M token ({kind}{OAUTH_TEXT.get(code, '')}){hint}"
        )
        return 1

    ok = True
    if c.get("iss") != advertised:
        ok = False
        print(f"FAIL: token iss {c.get('iss')!r} != discovery issuer {advertised!r}")
        if idp == "okta":
            print(
                "  → use a custom authorization server issuer "
                "(https://<org>.okta.com/oauth2/<authServerId>), not the org one"
            )
        elif urlparse(str(c.get("iss") or "")).hostname == "sts.windows.net":
            print(
                "  → the app issues v1 tokens; set api.requestedAccessTokenVersion=2 on "
                "the app registration (az rest PATCH /applications/<object-id>)"
            )
    aud = c.get("aud")
    aud = aud[0] if isinstance(aud, list) and aud else aud
    if aud != audience:
        ok = False
        print(f"FAIL: token aud {aud!r} is not the allowed audience {audience!r}")
        if idp == "okta":
            print(
                "  → set identity.audience to the authorization server's Audience "
                "(Security > API > Authorization Servers > Settings)"
            )
    if ok:
        print(f"OK: direct — {advertised} issues tokens for audience {audience}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
