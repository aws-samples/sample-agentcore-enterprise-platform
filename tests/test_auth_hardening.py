"""Synthesized Cognito behavior for workshop and enterprise brokered identity."""

from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.auth_stack import AuthStack

ACCOUNT = "111111111111"
REGION = "us-east-1"


def _synth_auth(
    idp_type: str = "cognito",
    idp_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    app = cdk.App()
    stack = AuthStack(
        app,
        f"Auth-{idp_type.replace('_', '-')}",
        project_name="agentcore",
        environment="test",
        idp_type=idp_type,
        idp_mode="brokered",
        idp_config=idp_config,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack).to_json()


def _resources(
    template: dict[str, Any],
    resource_type: str,
) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _user_client(
    template: dict[str, Any],
    client_name: str,
) -> dict[str, Any]:
    return next(
        resource["Properties"]
        for resource in _resources(template, "AWS::Cognito::UserPoolClient")
        if resource["Properties"]["ClientName"] == client_name
    )


def test_cognito_only_preserves_workshop_signup_and_native_auth() -> None:
    template = _synth_auth()
    user_pool = _resources(template, "AWS::Cognito::UserPool")[0]["Properties"]

    assert user_pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is False

    app_client = _user_client(template, "agentcore-test-app-client")
    web_client = _user_client(template, "agentcore-test-web-client")

    assert app_client["SupportedIdentityProviders"] == ["COGNITO"]
    assert web_client["SupportedIdentityProviders"] == ["COGNITO"]
    assert "ALLOW_USER_PASSWORD_AUTH" in app_client["ExplicitAuthFlows"]
    assert "ALLOW_USER_SRP_AUTH" in app_client["ExplicitAuthFlows"]
    assert "ALLOW_USER_SRP_AUTH" in web_client["ExplicitAuthFlows"]


@pytest.mark.parametrize(
    ("idp_type", "idp_config", "provider_name"),
    [
        (
            "entra_id",
            {
                "tenant_id": "22222222-3333-4444-5555-666666666666",
                "client_id": "entra-client",
                "client_secret_name": "agentcore/entra-secret",
            },
            "EntraID",
        ),
        (
            "okta",
            {
                "issuer_url": "https://example.okta.com/oauth2/default",
                "client_id": "okta-client",
                "client_secret_name": "agentcore/okta-secret",
            },
            "Okta",
        ),
        (
            "ping",
            {
                "issuer_url": "https://auth.example.com",
                "client_id": "ping-client",
                "client_secret_name": "agentcore/ping-secret",
            },
            "PingIdentity",
        ),
    ],
)
def test_brokered_enterprise_idp_is_the_only_user_sign_in_provider(
    idp_type: str,
    idp_config: dict[str, str],
    provider_name: str,
) -> None:
    template = _synth_auth(idp_type, idp_config)
    user_pool = _resources(template, "AWS::Cognito::UserPool")[0]["Properties"]

    assert user_pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True

    for client_name in (
        "agentcore-test-app-client",
        "agentcore-test-web-client",
    ):
        client = _user_client(template, client_name)
        assert client["SupportedIdentityProviders"] == [provider_name]
        assert "COGNITO" not in client["SupportedIdentityProviders"]
        assert "ALLOW_USER_PASSWORD_AUTH" not in client["ExplicitAuthFlows"]
        assert "ALLOW_USER_SRP_AUTH" not in client["ExplicitAuthFlows"]


@pytest.mark.parametrize(
    ("idp_type", "idp_config", "missing_key"),
    [
        (
            "entra_id",
            {
                "client_id": "entra-client",
                "client_secret_name": "agentcore/entra-secret",
            },
            "idp_tenant_id",
        ),
        (
            "okta",
            {
                "client_id": "okta-client",
                "client_secret_name": "agentcore/okta-secret",
            },
            "idp_issuer_url",
        ),
        (
            "ping",
            {
                "client_id": "ping-client",
                "client_secret_name": "agentcore/ping-secret",
            },
            "idp_issuer_url",
        ),
    ],
)
def test_incomplete_enterprise_idp_config_fails_closed(
    idp_type: str,
    idp_config: dict[str, str],
    missing_key: str,
) -> None:
    with pytest.raises(ValueError, match=missing_key):
        _synth_auth(idp_type, idp_config)


@pytest.mark.parametrize("idp_type", ["cognito", "entra_id", "okta", "ping"])
def test_public_web_client_uses_authorization_code_flow_for_pkce(
    idp_type: str,
) -> None:
    configs = {
        "cognito": None,
        "entra_id": {
            "tenant_id": "22222222-3333-4444-5555-666666666666",
            "client_id": "entra-client",
            "client_secret_name": "agentcore/entra-secret",
        },
        "okta": {
            "issuer_url": "https://example.okta.com/oauth2/default",
            "client_id": "okta-client",
            "client_secret_name": "agentcore/okta-secret",
        },
        "ping": {
            "issuer_url": "https://auth.example.com",
            "client_id": "ping-client",
            "client_secret_name": "agentcore/ping-secret",
        },
    }
    template = _synth_auth(idp_type, configs[idp_type])
    web_client = _user_client(template, "agentcore-test-web-client")

    assert web_client["GenerateSecret"] is False
    assert web_client["AllowedOAuthFlows"] == ["code"]
    assert web_client["AllowedOAuthFlowsUserPoolClient"] is True


# --- Runtime JWT verification (agent-code/shared/auth.py) -------------------

AGENT_SHARED = Path(__file__).resolve().parents[1] / "agent-code" / "shared"
JWT_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
JWT_CLIENT = "app-client-id"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def auth_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """auth.py with bedrock_agentcore stubbed and shared/__init__ bypassed."""
    pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    identity_auth = types.ModuleType("bedrock_agentcore.identity.auth")
    identity_auth.requires_access_token = lambda **_: lambda fn: fn
    runtime = types.ModuleType("bedrock_agentcore.runtime")
    runtime.RequestContext = object
    shared = types.ModuleType("shared")
    shared.__path__ = [str(AGENT_SHARED)]
    stubs = {
        "bedrock_agentcore": types.ModuleType("bedrock_agentcore"),
        "bedrock_agentcore.identity": types.ModuleType("bedrock_agentcore.identity"),
        "bedrock_agentcore.identity.auth": identity_auth,
        "bedrock_agentcore.runtime": runtime,
        "shared": shared,
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(
        sys.modules,
        "shared.jwt_claims",
        _load("shared.jwt_claims", AGENT_SHARED / "jwt_claims.py"),
    )
    auth = _load("shared_auth_under_test", AGENT_SHARED / "auth.py")
    monkeypatch.setattr(auth, "COGNITO_ISSUER_URL", JWT_ISSUER)
    monkeypatch.setattr(auth, "ALLOWED_CLIENTS", (JWT_CLIENT,))
    return auth


@pytest.fixture
def sign(auth_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth_module, "_signing_key", lambda _token: key.public_key())

    def _sign(claims: dict[str, Any]) -> str:
        return jwt.encode(claims, key, algorithm="RS256")

    return _sign


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": JWT_ISSUER,
        "aud": JWT_CLIENT,
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def _verify(auth: types.ModuleType, token: str) -> str:
    context = types.SimpleNamespace(
        request_headers={"Authorization": f"Bearer {token}"}
    )
    return auth.extract_user_id_from_context(context)


def test_valid_token_is_accepted(auth_module, sign) -> None:
    assert _verify(auth_module, sign(_claims())) == "user-1"


def test_token_without_exp_is_rejected(auth_module, sign) -> None:
    claims = _claims()
    del claims["exp"]
    with pytest.raises(ValueError, match="JWT verification failed"):
        _verify(auth_module, sign(claims))


def test_expired_token_is_rejected(auth_module, sign) -> None:
    now = int(time.time())
    token = sign(_claims(iat=now - 600, exp=now - 60))
    with pytest.raises(ValueError, match="JWT verification failed"):
        _verify(auth_module, token)


def test_list_audience_with_our_client_is_accepted(auth_module, sign) -> None:
    token = sign(_claims(aud=["other-api", JWT_CLIENT]))
    assert _verify(auth_module, token) == "user-1"


def test_list_audience_without_our_client_is_rejected(auth_module, sign) -> None:
    token = sign(_claims(aud=["other-api", "another"]))
    with pytest.raises(ValueError, match="client"):
        _verify(auth_module, token)


def test_alg_none_token_is_rejected(auth_module, sign) -> None:
    import jwt

    token = jwt.encode(_claims(), None, algorithm="none")
    with pytest.raises(ValueError, match="JWT verification failed"):
        _verify(auth_module, token)


def test_hs256_token_is_rejected(auth_module, sign) -> None:
    import jwt

    token = jwt.encode(_claims(), "s" * 64, algorithm="HS256")
    with pytest.raises(ValueError, match="JWT verification failed"):
        _verify(auth_module, token)


def test_discovery_failure_is_retried_on_next_request(
    auth_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses: list[Any] = [
        OSError("discovery down"),
        {"jwks_uri": "https://keys.example.com/jwks"},
    ]

    def discover() -> dict[str, str]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(auth_module, "_discover", discover)
    monkeypatch.setattr(auth_module, "_jwks_cache", auth_module.SuccessCache(ttl=3600))

    first = auth_module._jwks_client()
    assert first.uri == f"{JWT_ISSUER}/.well-known/jwks.json"
    second = auth_module._jwks_client()
    assert second.uri == "https://keys.example.com/jwks"
    assert auth_module._jwks_client() is second
