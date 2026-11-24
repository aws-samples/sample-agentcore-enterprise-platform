"""Synth: Okta direct mode deploys no Cognito pool and pins the AS audience."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.auth_stack import AuthStack
from stacks.gateway_stack import GatewayStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")
ISSUER = "https://acme.okta.com/oauth2/default"
OKTA = {
    "issuer_url": ISSUER,
    "client_id": "0oa1abcdefg",
    "client_secret_name": "agentcore/okta-client-secret",
    "audience": "api://agentcore",
    "m2m_scope": "agentcore.invoke",
}


def _auth(app, idp_type="okta", config=None):
    return AuthStack(
        app,
        "t-auth",
        project_name="p",
        environment="dev",
        idp_type=idp_type,
        idp_mode="direct",
        idp_config=config if config is not None else dict(OKTA),
        env=ENV,
    )


def test_okta_direct_auth_stack_has_no_user_pool():
    app = cdk.App()
    auth = _auth(app)
    template = Template.from_stack(auth)
    template.resource_count_is("AWS::Cognito::UserPool", 0)
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/p/dev/auth/m2m-scope", "Value": "agentcore.invoke"},
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter", {"Name": "/p/dev/auth/issuer-url", "Value": ISSUER}
    )
    assert auth.discovery_url == f"{ISSUER}/.well-known/openid-configuration"
    assert auth.direct_audience == "api://agentcore"
    assert auth.direct_m2m_scope == "agentcore.invoke"


def test_okta_direct_refuses_org_issuer_from_raw_context():
    with pytest.raises(ValueError, match="custom authorization"):
        _auth(cdk.App(), config={**OKTA, "issuer_url": "https://acme.okta.com"})


@pytest.mark.parametrize("key", ["audience", "m2m_scope"])
def test_okta_direct_requires_audience_and_scope(key):
    with pytest.raises(ValueError, match=f"idp_{key}"):
        _auth(cdk.App(), config={**OKTA, key: ""})


def test_entra_direct_scope_unchanged():
    auth = _auth(
        cdk.App(),
        idp_type="entra_id",
        config={
            "tenant_id": "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "client_id": "abc",
            "client_secret_name": "agentcore/idp-client-secret",
        },
    )
    Template.from_stack(auth).has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/p/dev/auth/m2m-scope", "Value": "abc/.default"},
    )
    assert auth.direct_audience == "abc"


def test_okta_direct_gateway_authorizer_pins_audience():
    app = cdk.App()
    auth = _auth(app)
    gateway = GatewayStack(
        app,
        "t-gateway",
        project_name="p",
        environment="dev",
        cognito_issuer_url=auth.issuer_url,
        cognito_allowed_clients=[],
        allowed_audience=[auth.direct_audience],
        enable_web_search=False,
        tool_configs={
            "sample-tool": {
                "source_dir": "tools/sample_tool",
                "env_vars": {},
                "tool_schema": [
                    {
                        "Name": "text_analysis_tool",
                        "Description": "Analyzes text.",
                        "InputSchema": {
                            "Type": "object",
                            "Properties": {
                                "text": {"Type": "string", "Description": "t"}
                            },
                            "Required": ["text"],
                        },
                    }
                ],
            }
        },
        enable_egress_filter=False,
        enable_cedar=False,
        cedar_mode="LOG_ONLY",
        debug_exceptions=True,
        kms_key_arn="",
        env=ENV,
    )
    rendered = json.dumps(Template.from_stack(gateway).to_json())
    assert f"{ISSUER}/.well-known/openid-configuration" in rendered
    assert "api://agentcore" in rendered
