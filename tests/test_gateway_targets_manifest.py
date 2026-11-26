"""gateway.tools in platform.yaml drives the gateway targets GatewayStack synthesizes."""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from pydantic import ValidationError

from infra_utils.platform_config import PlatformConfig, gateway_tool_configs
from stacks.gateway_stack import DEFAULT_TOOL_CONFIGS, GatewayStack

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "openapi" / "sample.json"
VAULT = "arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default"
API_KEY_ARN = f"{VAULT}/apikeycredentialprovider/weather-key"
OAUTH_ARN = f"{VAULT}/oauth2credentialprovider/crm-oauth"


@pytest.fixture(autouse=True)
def _repo_cwd(monkeypatch):
    # Lambda asset paths (tools/sample_tool) resolve from the repo root.
    monkeypatch.chdir(REPO)


def _synth(tool_configs):
    app = cdk.App()
    stack = GatewayStack(
        app,
        "test-gateway",
        project_name="agentcore-workshop",
        environment="dev",
        cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST",
        cognito_allowed_clients=["client"],
        tool_configs=tool_configs,
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _targets(template):
    resources = template.find_resources("AWS::BedrockAgentCore::GatewayTarget")
    return {
        r["Properties"]["Name"]: (lid, r["Properties"]) for lid, r in resources.items()
    }


def _openapi(name, provider):
    return {
        "name": name,
        "type": "openapi",
        "schema_path": str(FIXTURE),
        "credential_provider": provider,
    }


def test_manifest_openapi_target_synthesizes_with_its_credential_provider():
    config = PlatformConfig.model_validate(
        {
            "gateway": {
                "tools": [
                    _openapi("weather-api", {"type": "api_key", "arn": API_KEY_ARN}),
                    _openapi(
                        "crm-api",
                        {"type": "oauth", "arn": OAUTH_ARN, "scopes": ["read"]},
                    ),
                ]
            }
        }
    )
    template = _synth(gateway_tool_configs(config.gateway.tools, DEFAULT_TOOL_CONFIGS))
    targets = _targets(template)
    assert set(targets) == {"weather-api", "crm-api"}

    _, weather = targets["weather-api"]
    payload = weather["TargetConfiguration"]["Mcp"]["OpenApiSchema"]["InlinePayload"]
    assert json.loads(payload) == json.loads(FIXTURE.read_text())
    assert weather["CredentialProviderConfigurations"] == [
        {
            "CredentialProviderType": "API_KEY",
            "CredentialProvider": {
                "ApiKeyCredentialProvider": {"ProviderArn": API_KEY_ARN}
            },
        }
    ]
    _, crm = targets["crm-api"]
    assert crm["CredentialProviderConfigurations"] == [
        {
            "CredentialProviderType": "OAUTH",
            "CredentialProvider": {
                "OauthCredentialProvider": {
                    "ProviderArn": OAUTH_ARN,
                    "Scopes": ["read"],
                }
            },
        }
    ]

    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "bedrock-agentcore:GetResourceApiKey" in policies
    assert API_KEY_ARN in policies and OAUTH_ARN in policies


def test_openapi_targets_read_only_the_secrets_behind_their_providers():
    config = PlatformConfig.model_validate(
        {
            "gateway": {
                "tools": [
                    _openapi("weather-api", {"type": "api_key", "arn": API_KEY_ARN}),
                    _openapi("crm-api", {"type": "oauth", "arn": OAUTH_ARN}),
                ]
            }
        }
    )
    template = _synth(gateway_tool_configs(config.gateway.tools, DEFAULT_TOOL_CONFIGS))
    statements = [
        s
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    secrets = [s for s in statements if s.get("Sid") == "GetTargetCredentialSecrets"]
    assert len(secrets) == 1
    assert secrets[0]["Action"] == "secretsmanager:GetSecretValue"
    prefix = (
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:"
        "bedrock-agentcore-identity!default"
    )
    assert sorted(secrets[0]["Resource"]) == sorted(
        [
            f"{prefix}/apikey/weather-key-??????",
            f"{prefix}/oauth2/crm-oauth-??????",
        ]
    )


def test_without_gateway_tools_the_sample_tool_lambda_still_deploys():
    config = PlatformConfig.model_validate({})
    tool_configs = gateway_tool_configs(config.gateway.tools, DEFAULT_TOOL_CONFIGS)
    assert tool_configs == DEFAULT_TOOL_CONFIGS
    template = _synth(tool_configs)
    targets = _targets(template)
    assert set(targets) == {"sample-tool"}
    logical_id, props = targets["sample-tool"]
    assert logical_id.startswith("Targetsampletool")
    lam = props["TargetConfiguration"]["Mcp"]["Lambda"]
    assert lam["ToolSchema"]["InlinePayload"][0]["Name"] == "text_analysis_tool"
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "GetResourceApiKey" not in policies
    assert "secretsmanager:GetSecretValue" not in policies


def test_invalid_entries_fail_with_every_error_reported():
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate(
            {
                "gateway": {
                    "tools": [
                        {"name": "x", "type": "graphql"},
                        {
                            "name": "missing-spec",
                            "type": "openapi",
                            "schema_path": "no/such/spec.json",
                            "credential_provider": {
                                "type": "api_key",
                                "arn": API_KEY_ARN,
                            },
                        },
                        _openapi("bad-arn", {"type": "api_key", "arn": "arn:aws:nope"}),
                        {
                            "name": "no-provider",
                            "type": "openapi",
                            "schema_path": str(FIXTURE),
                        },
                    ]
                }
            }
        )
    msg = str(excinfo.value)
    assert "'lambda' or 'openapi'" in msg
    assert "'no/such/spec.json' is not a file" in msg
    assert "not an AgentCore Identity credential provider ARN" in msg
    assert "requires credential_provider" in msg


def test_duplicate_target_names_fail():
    with pytest.raises(ValidationError, match="duplicate target names"):
        PlatformConfig.model_validate(
            {
                "gateway": {
                    "tools": [
                        "sample-tool",
                        _openapi(
                            "sample-tool", {"type": "api_key", "arn": API_KEY_ARN}
                        ),
                    ]
                }
            }
        )
