"""Synthesized Cognito behavior for workshop and enterprise brokered identity."""

from __future__ import annotations

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
