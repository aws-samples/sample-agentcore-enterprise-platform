"""Synth tests for the Cedar policy engine wiring in GatewayStack."""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.gateway_stack import GatewayStack

ACCOUNT = "111122223333"
REGION = "us-east-1"
# Any existing Lambda source directory works as the tool asset; reuse one in the repo
# so the test needs no temporary directory.
TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "egress_interceptor"


def _synth(project_name="proj"):
    tool_dir = TOOL_DIR
    app = cdk.App()
    stack = GatewayStack(
        app,
        f"{project_name}-gateway",
        project_name=project_name,
        environment="dev",
        cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
        cognito_allowed_clients=["client-id"],
        tool_configs={
            "sample-tool": {"source_dir": str(tool_dir), "tool_schema": []},
        },
        enable_web_search=True,
        enable_cedar=True,
        cedar_mode="ENFORCE",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack).to_json()


def _of_type(template, cfn_type):
    return {
        logical_id: res
        for logical_id, res in template["Resources"].items()
        if res["Type"] == cfn_type
    }


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _statements(template):
    out = []
    for res in _of_type(template, "AWS::IAM::Policy").values():
        out.extend(res["Properties"]["PolicyDocument"]["Statement"])
    return out


def _single(template, cfn_type):
    found = _of_type(template, cfn_type)
    assert len(found) == 1, f"expected one {cfn_type}, got {list(found)}"
    return next(iter(found.items()))


def test_gateway_role_has_policy_engine_actions():
    template = _synth()
    engine_id, _ = _single(template, "AWS::BedrockAgentCore::PolicyEngine")
    engine_arn = {"Fn::GetAtt": [engine_id, "PolicyEngineArn"]}
    gateways = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/*"

    statements = _statements(template)

    def resources_for(action):
        found = []
        for s in statements:
            if action in _as_list(s["Action"]):
                found.extend(_as_list(s["Resource"]))
        return found

    assert engine_arn in resources_for("bedrock-agentcore:GetPolicyEngine")
    for action in (
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
    ):
        resources = resources_for(action)
        assert engine_arn in resources
        assert gateways in resources


def test_policy_depends_on_gateway_and_all_targets():
    template = _synth()
    gateway_id, _ = _single(template, "AWS::BedrockAgentCore::Gateway")
    targets = _of_type(template, "AWS::BedrockAgentCore::GatewayTarget")
    assert len(targets) == 2  # Lambda tool + web-search
    _, policy = _single(template, "AWS::BedrockAgentCore::Policy")
    depends_on = policy.get("DependsOn", [])
    assert gateway_id in depends_on
    for target_id in targets:
        assert target_id in depends_on


def test_policy_statement_references_gateway_arn():
    template = _synth()
    gateway_id, _ = _single(template, "AWS::BedrockAgentCore::Gateway")
    _, policy = _single(template, "AWS::BedrockAgentCore::Policy")
    definition = json.dumps(policy["Properties"]["Definition"])
    assert json.dumps({"Fn::GetAtt": [gateway_id, "GatewayArn"]}) in definition
    assert "AgentCore::Gateway::" in definition
    assert "<<" not in definition


def test_policy_name_is_unique_per_project():
    _, a = _single(_synth("proj-a"), "AWS::BedrockAgentCore::Policy")
    _, b = _single(_synth("proj-b"), "AWS::BedrockAgentCore::Policy")
    name_a = a["Properties"]["Name"]
    name_b = b["Properties"]["Name"]
    assert name_a == "proj_a_dev_PermitReadTools"
    assert name_b == "proj_b_dev_PermitReadTools"
    assert name_a != name_b


def test_policy_depends_on_openapi_targets():
    from infra_utils.platform_config import PlatformConfig, gateway_tool_configs
    from stacks.gateway_stack import DEFAULT_TOOL_CONFIGS

    fixture = Path(__file__).resolve().parent / "fixtures" / "openapi" / "sample.json"
    arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default"
        "/apikeycredentialprovider/weather-key"
    )
    config = PlatformConfig.model_validate(
        {
            "gateway": {
                "tools": [
                    {
                        "name": "weather-api",
                        "type": "openapi",
                        "schema_path": str(fixture),
                        "credential_provider": {"type": "api_key", "arn": arn},
                    }
                ]
            }
        }
    )
    app = cdk.App()
    stack = GatewayStack(
        app,
        "proj-gateway",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
        cognito_allowed_clients=["client-id"],
        tool_configs=gateway_tool_configs(config.gateway.tools, DEFAULT_TOOL_CONFIGS),
        enable_cedar=True,
        cedar_mode="ENFORCE",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    targets = _of_type(template, "AWS::BedrockAgentCore::GatewayTarget")
    openapi = [
        lid for lid, r in targets.items() if r["Properties"]["Name"] == "weather-api"
    ]
    assert openapi
    policies = _of_type(template, "AWS::BedrockAgentCore::Policy").values()
    assert policies
    for policy in policies:
        assert openapi[0] in policy.get("DependsOn", [])
