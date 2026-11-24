"""Gateway must be created only after its role's policy (KMS grant) exists."""

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.gateway_stack import GatewayStack

KMS_ARN = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
KMS_ACTIONS = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]


def _synth(kms_key_arn: str = "") -> Template:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "TestGateway",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test",
        cognito_allowed_clients=["client-id"],
        tool_configs=None,
        kms_key_arn=kms_key_arn,
    )
    return Template.from_stack(stack)


def _role_policy_id(template: Template) -> tuple[str, dict]:
    roles = template.find_resources("AWS::IAM::Role")
    role_ids = [
        rid
        for rid, res in roles.items()
        if res["Properties"].get("RoleName") == "proj-dev-gateway-role"
    ]
    assert len(role_ids) == 1
    role_id = role_ids[0]
    policies = template.find_resources("AWS::IAM::Policy")
    matches = [
        (pid, res)
        for pid, res in policies.items()
        if {"Ref": role_id} in res["Properties"].get("Roles", [])
    ]
    assert len(matches) == 1
    return matches[0]


def _gateway(template: Template) -> dict:
    gateways = template.find_resources("AWS::BedrockAgentCore::Gateway")
    assert len(gateways) == 1
    return next(iter(gateways.values()))


def test_gateway_depends_on_role_policy_with_kms():
    template = _synth(KMS_ARN)
    policy_id, policy = _role_policy_id(template)

    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    kms_statements = [
        s
        for s in statements
        if sorted(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        == KMS_ACTIONS
    ]
    assert len(kms_statements) == 1
    assert kms_statements[0]["Resource"] == KMS_ARN
    assert kms_statements[0]["Effect"] == "Allow"

    gateway = _gateway(template)
    assert gateway["Properties"]["KmsKeyArn"] == KMS_ARN
    assert policy_id in gateway.get("DependsOn", [])


def test_gateway_depends_on_role_policy_without_kms():
    template = _synth()
    policy_id, _ = _role_policy_id(template)

    gateway = _gateway(template)
    assert "KmsKeyArn" not in gateway["Properties"]
    assert policy_id in gateway.get("DependsOn", [])
