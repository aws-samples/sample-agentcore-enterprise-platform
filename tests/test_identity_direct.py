"""identity.mode: direct — the IdP issues tokens, no Cognito.

Config rules, the Design read-back, the shared authorizer shape, the
client-side token path and the container-side helpers are unit-tested here;
the templates themselves are proven by scripts/check-contract.sh synthesizing
tests/fixtures/contract/direct.yaml (CI) and by the live rig.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
# jwt_claims is stdlib-only (importing shared/ would pull bedrock_agentcore,
# which the CI test job does not install) — same trick as test_jwt_claims.py.
sys.path.insert(0, str(REPO / "agent-code" / "shared"))

import utils
from jwt_claims import gateway_token_scopes, jwks_uri

from infra_utils.jwt_authorizer import custom_jwt_authorizer
from infra_utils.platform_config import (
    PlatformConfig,
    design_plan,
    to_env,
)

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DIRECT = {
    "deployment": {"platform_account": "111122223333"},
    "identity": {
        "idp": "entra_id",
        "mode": "direct",
        "tenant_id": TENANT,
        "client_id": CLIENT,
        "client_secret_name": "agentcore/idp-client-secret",
    },
}


# ── contract ──


def test_default_mode_is_brokered_and_absent_from_old_files():
    c = PlatformConfig()
    assert c.identity.mode == "brokered"
    assert to_env(c)["IDP_MODE"] == "brokered"


def test_direct_refuses_ping():
    # Okta direct is supported (tests/test_identity_okta_direct.py); Ping is next.
    with pytest.raises(ValidationError, match="supported for idp 'entra_id'"):
        PlatformConfig.model_validate(
            {
                "identity": {
                    "idp": "ping",
                    "mode": "direct",
                    "issuer_url": "https://auth.pingone.com/abc/as",
                    "client_id": "abc",
                    "client_secret_name": "agentcore/ping",
                }
            }
        )


def test_direct_derives_issuer_and_scope():
    c = PlatformConfig.model_validate(DIRECT)
    assert c.identity.direct_issuer_url == (
        f"https://login.microsoftonline.com/{TENANT}/v2.0"
    )
    assert c.identity.direct_m2m_scope == f"{CLIENT}/.default"
    assert to_env(c)["IDP_MODE"] == "direct"


# ── design read-back ──


def test_design_prints_direct_issuer_and_prereqs():
    t = "\n".join(design_plan(PlatformConfig.model_validate(DIRECT)))
    assert "Sign-in: entra_id (direct" in t
    assert f"issuer: https://login.microsoftonline.com/{TENANT}/v2.0" in t
    assert f"audience: {CLIENT}" in t
    assert "service principal" in t and "v2 access tokens" in t
    assert "amazoncognito.com" not in t


def test_design_prints_cognito_redirect_uri_for_brokered_idp():
    c = PlatformConfig.model_validate(
        {
            "deployment": DIRECT["deployment"],
            "identity": {**DIRECT["identity"], "mode": "brokered"},
        }
    )
    t = "\n".join(design_plan(c, account="444333641315"))
    assert "Sign-in: entra_id via Cognito (brokered)" in t
    assert (
        "https://agentcore-workshop-dev-444333641315.auth.us-east-1"
        ".amazoncognito.com/oauth2/idpresponse" in t
    )
    assert "<account-id>" in "\n".join(design_plan(c))  # no account known yet


# ── authorizer shape ──


def test_authorizer_pins_clients_or_audience_never_neither():
    brokered = custom_jwt_authorizer("https://iss", ["app", "m2m"])
    assert brokered["customJwtAuthorizer"] == {
        "discoveryUrl": "https://iss/.well-known/openid-configuration",
        "allowedClients": ["app", "m2m"],
    }
    direct = custom_jwt_authorizer("https://iss/", [], [CLIENT])
    assert direct["customJwtAuthorizer"] == {
        "discoveryUrl": "https://iss/.well-known/openid-configuration",
        "allowedAudience": [CLIENT],
    }
    with pytest.raises(ValueError, match="every token"):
        custom_jwt_authorizer("https://iss", [], [])


def test_both_stacks_use_the_shared_authorizer():
    for name in ("gateway_stack.py", "runtime_stack.py"):
        src = (REPO / "stacks" / name).read_text()
        assert "custom_jwt_authorizer(" in src, name
        assert '"allowedClients"' not in src, f"{name} renders its own authorizer"


def test_app_switches_to_audience_and_scope_in_direct_mode():
    src = (REPO / "app.py").read_text()
    assert 'idp_mode = cfg("idp_mode", "IDP_MODE", "brokered")' in src
    # Entra pins its client id, Okta the authorization server's audience —
    # both resolved by the auth stack (stacks/auth_stack.py direct_audience).
    assert "allowed_clients, allowed_audience = [], [auth_stack.direct_audience]" in src
    assert "gateway_token_scopes = auth_stack.direct_m2m_scope" in src
    # Empty env values are rejected at deploy time — the scope var is conditional.
    assert (
        '{"GATEWAY_TOKEN_SCOPES": gateway_token_scopes} if gateway_token_scopes' in src
    )


def test_auth_stack_publishes_the_same_interface_in_both_modes():
    src = (REPO / "stacks" / "auth_stack.py").read_text()
    for key in ('"mode"', '"issuer-url"', '"m2m-client-id"', '"m2m-scope"'):
        assert src.count(key) >= 2, f"{key} must be published in brokered AND direct"
    assert src.count('"m2m-client-secret-name"') >= 2
    assert 'self._direct_secret_name = idp_config["client_secret_name"]' in src


# ── container side ──


def test_gateway_token_scopes_from_env():
    assert gateway_token_scopes({}) == []
    assert gateway_token_scopes({"GATEWAY_TOKEN_SCOPES": ""}) == []
    assert gateway_token_scopes({"GATEWAY_TOKEN_SCOPES": f"{CLIENT}/.default"}) == [
        f"{CLIENT}/.default"
    ]


def test_every_gateway_tool_requests_env_scopes():
    files = list((REPO / "agent-code").glob("*/tools/gateway.py")) + [
        REPO / "agent-code" / "shared" / "auth.py"
    ]
    assert len(files) >= 6
    for f in files:
        src = f.read_text()
        assert "scopes=gateway_token_scopes()" in src, f
        assert "scopes=[]" not in src, f


def test_jwks_uri_prefers_discovery_and_falls_back_to_cognito_layout():
    entra = {
        "jwks_uri": f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    }
    assert jwks_uri("https://x/v2.0", entra) == entra["jwks_uri"]
    assert jwks_uri("https://cognito-idp.us-east-1.amazonaws.com/p/", None) == (
        "https://cognito-idp.us-east-1.amazonaws.com/p/.well-known/jwks.json"
    )


# ── client side: get_m2m_token direct branch ──


class FakeSsm:
    params: ClassVar[dict[str, str]] = {
        "auth/mode": "direct",
        "auth/issuer-url": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "auth/m2m-client-id": CLIENT,
        "auth/m2m-scope": f"{CLIENT}/.default",
        "auth/m2m-client-secret-name": "agentcore/idp-client-secret",
    }

    def get_parameter(self, Name):
        key = Name.split("/", 3)[3]
        return {"Parameter": {"Value": self.params[key]}}

    def get_secret_value(self, SecretId):
        assert SecretId == "agentcore/idp-client-secret"
        return {"SecretString": "stub-secret-from-fake-secretsmanager"}


def test_direct_token_uses_discovered_endpoint_and_app_default_scope(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "proj")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(utils.boto3, "client", lambda *a, **kw: FakeSsm())
    seen = {}

    def fake_urlopen(req, *a, **kw):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/.well-known/openid-configuration"):
            body = {
                "token_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
            }
        else:
            seen["url"] = url
            seen["body"] = dict(p.split("=", 1) for p in req.data.decode().split("&"))
            seen["auth"] = req.get_header("Authorization")
            body = {"access_token": "stub-token-from-fake-idp"}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(utils.urllib.request, "urlopen", fake_urlopen)
    assert utils.get_m2m_token() == "stub-token-from-fake-idp"
    assert (
        seen["url"] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    )
    assert seen["body"]["grant_type"] == "client_credentials"
    assert seen["body"]["client_id"] == CLIENT
    assert seen["body"]["scope"] == f"{CLIENT}%2F.default"
    # Entra keeps client_secret_post: no Basic header.
    assert seen["auth"] is None


def test_missing_mode_parameter_means_brokered(monkeypatch):
    class NoMode(FakeSsm):
        def get_parameter(self, Name):
            if Name.endswith("auth/mode"):
                raise RuntimeError("ParameterNotFound")
            return super().get_parameter(Name)

    monkeypatch.setattr(utils.boto3, "client", lambda *a, **kw: NoMode())
    assert utils.auth_mode("proj", "dev") == "brokered"


def test_federated_workload_uses_okta_audience_and_scope():
    src = (REPO / "app.py").read_text()
    fed_branch = src.split("if is_fed_workload:\n    issuer_url", 1)[1].split(
        "\nelse:", 1
    )[0]
    assert 'allowed_audience = [idp_config["audience"]]' in fed_branch
    assert 'gateway_token_scopes = idp_config["m2m_scope"]' in fed_branch


def test_check_identity_reads_okta_audience_from_platform_yaml(monkeypatch):
    monkeypatch.delenv("PLATFORM_ALLOW_PLACEHOLDERS", raising=False)
    import check_identity

    okta = PlatformConfig.model_validate(
        {
            "deployment": {"platform_account": "111122223333"},
            "identity": {
                "idp": "okta",
                "mode": "direct",
                "issuer_url": "https://acme.okta.com/oauth2/default",
                "client_id": "0oa1abcdefg",
                "client_secret_name": "agentcore/okta-client-secret",
                "audience": "api://agentcore",
                "m2m_scope": "agentcore.invoke",
            },
        }
    )
    # An existing file stands in for platform.yaml; the loader is stubbed so
    # the test needs no temporary directory.
    cfg = REPO / "app.py"
    loaded = []

    def fake_load(path):
        loaded.append(path)
        return okta

    monkeypatch.setattr(check_identity, "load_platform_config", fake_load)
    assert (
        check_identity.configured_audience({"PLATFORM_CONFIG": str(cfg)})
        == "api://agentcore"
    )
    assert loaded == [str(cfg)]
    assert (
        check_identity.configured_audience(
            {"PLATFORM_CONFIG": str(cfg), "IDP_AUDIENCE": "api://override"}
        )
        == "api://override"
    )
    missing = REPO / "does-not-exist-platform.yaml"
    assert check_identity.configured_audience({"PLATFORM_CONFIG": str(missing)}) == ""


def test_check_identity_prints_okta_hint_from_http_error(monkeypatch, capsys):
    import urllib.error

    import check_identity

    issuer = "https://acme.okta.com/oauth2/default"
    monkeypatch.setenv("IDP_MODE", "direct")
    monkeypatch.setenv("IDP_TYPE", "okta")
    monkeypatch.setenv("IDP_AUDIENCE", "api://agentcore")
    monkeypatch.setattr(check_identity, "auth_mode", lambda p, e: "direct")
    monkeypatch.setattr(check_identity, "get_ssm_param", lambda *a, **k: issuer)
    monkeypatch.setattr(check_identity, "oidc_discovery", lambda i: {"issuer": i})
    body = json.dumps(
        {"error": "invalid_scope", "error_description": "free text not printed"}
    ).encode()

    def fail(*a, **k):
        raise urllib.error.HTTPError(
            f"{issuer}/v1/token", 400, "Bad Request", None, io.BytesIO(body)
        )

    monkeypatch.setattr(check_identity, "get_m2m_token", fail)
    assert check_identity.main() == 1
    out = capsys.readouterr().out
    assert "OAuth error: invalid_scope" in out
    assert "define the custom scope" in out
    assert "free text not printed" not in out


def test_oauth_error_code_ignores_non_http_errors():
    import check_identity

    assert check_identity.oauth_error_code(RuntimeError("invalid_scope")) == ""


def test_check_identity_decodes_claims():
    sys.path.insert(0, str(REPO / "scripts"))
    import check_identity

    payload = base64.urlsafe_b64encode(json.dumps({"aud": CLIENT}).encode()).rstrip(
        b"="
    )
    assert check_identity.claims(f"h.{payload.decode()}.s") == {"aud": CLIENT}
