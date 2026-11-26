"""Production mode changes lifecycle and disclosure behavior in synthesized IaC."""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra_utils.platform_config import resolve_deployment_mode
from stacks.auth_stack import AuthStack
from stacks.gateway_stack import GatewayStack
from stacks.memory_stack import MemoryStack
from stacks.observability_stack import ObservabilityStack
from stacks.runtime_stack import RuntimeStack
from stacks.security_stack import SecurityStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")


def resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def synth(stack: cdk.Stack) -> dict[str, Any]:
    return Template.from_stack(stack).to_json()


def test_auth_retains_identity_data_and_enables_deletion_protection() -> None:
    app = cdk.App()
    template = synth(
        AuthStack(
            app,
            "ProductionAuth",
            project_name="prodtest",
            environment="prod",
            idp_type="entra_id",
            idp_config={
                "tenant_id": "11111111-2222-3333-4444-555555555555",
                "client_id": "client-id",
                "client_secret_name": "agentcore/idp-client-secret",
            },
            retain_data=True,
            env=ENV,
        )
    )
    pool = resources(template, "AWS::Cognito::UserPool")[0]
    assert pool["DeletionPolicy"] == "Retain"
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert pool["Properties"]["DeletionProtection"] == "ACTIVE"
    assert all(
        secret["DeletionPolicy"] == "Retain"
        for secret in resources(template, "AWS::SecretsManager::Secret")
    )


def test_security_retains_and_encrypts_a_multi_region_audit_trail() -> None:
    app = cdk.App()
    template = synth(
        SecurityStack(
            app,
            "ProductionSecurity",
            project_name="prodtest",
            environment="prod",
            retain_data=True,
            env=ENV,
        )
    )
    key = resources(template, "AWS::KMS::Key")[0]
    bucket = resources(template, "AWS::S3::Bucket")[0]
    trail = resources(template, "AWS::CloudTrail::Trail")[0]["Properties"]
    assert key["DeletionPolicy"] == "Retain"
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    encryption = bucket["Properties"]["BucketEncryption"][
        "ServerSideEncryptionConfiguration"
    ][0]["ServerSideEncryptionByDefault"]
    assert encryption["SSEAlgorithm"] == "aws:kms"
    assert trail["EnableLogFileValidation"] is True
    assert trail["IsMultiRegionTrail"] is True
    assert not resources(template, "Custom::S3AutoDeleteObjects")


def test_production_trail_uses_the_cmk_with_cloudtrail_key_policy() -> None:
    app = cdk.App()
    template = synth(
        SecurityStack(
            app,
            "ProductionSecurityTrailKms",
            project_name="prodtest",
            environment="prod",
            retain_data=True,
            env=ENV,
        )
    )
    key_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::KMS::Key"
    )
    trail = resources(template, "AWS::CloudTrail::Trail")[0]["Properties"]
    assert trail["KmsKeyId"] == {"Fn::GetAtt": [key_id, "Arn"]}

    statements = template["Resources"][key_id]["Properties"]["KeyPolicy"]["Statement"]
    by_sid = {statement.get("Sid"): statement for statement in statements}
    encrypt = by_sid["AllowCloudTrailEncryptLogs"]
    assert encrypt["Principal"] == {"Service": "cloudtrail.amazonaws.com"}
    assert encrypt["Action"] == "kms:GenerateDataKey*"
    assert "aws:SourceArn" in encrypt["Condition"]["StringEquals"]
    assert (
        "kms:EncryptionContext:aws:cloudtrail:arn" in encrypt["Condition"]["StringLike"]
    )
    describe = by_sid["AllowCloudTrailDescribeKey"]
    assert describe["Principal"] == {"Service": "cloudtrail.amazonaws.com"}
    assert describe["Action"] == "kms:DescribeKey"
    assert "aws:SourceArn" in describe["Condition"]["StringEquals"]


def test_workshop_trail_does_not_use_sse_kms() -> None:
    app = cdk.App()
    template = synth(
        SecurityStack(
            app,
            "WorkshopSecurity",
            project_name="workshoptest",
            environment="dev",
            env=ENV,
        )
    )
    trail = resources(template, "AWS::CloudTrail::Trail")[0]["Properties"]
    assert "KmsKeyId" not in trail


def test_memory_and_logs_are_retained_for_bounded_periods() -> None:
    app = cdk.App()
    memory = synth(
        MemoryStack(
            app,
            "ProductionMemory",
            project_name="prodtest",
            environment="prod",
            event_expiry_days=30,
            retain_data=True,
            env=ENV,
        )
    )
    memory_resource = resources(memory, "AWS::BedrockAgentCore::Memory")[0]
    assert memory_resource["DeletionPolicy"] == "Retain"
    assert memory_resource["Properties"]["EventExpiryDuration"] == 30

    app = cdk.App()
    observability = synth(
        ObservabilityStack(
            app,
            "ProductionObservability",
            project_name="prodtest",
            environment="prod",
            monitored_resources={
                "memory": (
                    "arn:aws:bedrock-agentcore:us-east-1:111111111111:memory/example"
                )
            },
            log_retention_days=90,
            retain_data=True,
            env=ENV,
        )
    )
    log_group = resources(observability, "AWS::Logs::LogGroup")[0]
    assert log_group["DeletionPolicy"] == "Retain"
    assert log_group["Properties"]["RetentionInDays"] == 90


def test_runtime_image_repository_is_retained() -> None:
    app = cdk.App()
    template = synth(
        RuntimeStack(
            app,
            "ProductionRuntime",
            project_name="prodtest",
            environment="prod",
            component_name="orchestrator",
            source_dir="tools/sample_tool",
            runtime_type="orchestrator",
            retain_data=True,
            env=ENV,
        )
    )
    repository = resources(template, "AWS::ECR::Repository")[0]
    assert repository["DeletionPolicy"] == "Retain"
    assert repository["UpdateReplacePolicy"] == "Retain"
    assert not resources(template, "Custom::ECRAutoDeleteImages")


def test_gateway_sanitizes_production_errors_and_uses_the_cmk() -> None:
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
    kms_key_reference = "test-key-reference"

    app = cdk.App()
    production = synth(
        GatewayStack(
            app,
            "ProductionGateway",
            project_name="prodtest",
            environment="prod",
            cognito_issuer_url=issuer,
            cognito_allowed_clients=["client"],
            kms_key_arn=kms_key_reference,
            debug_exceptions=False,
            env=ENV,
        )
    )
    gateway = resources(production, "AWS::BedrockAgentCore::Gateway")[0]["Properties"]
    assert "ExceptionLevel" not in gateway
    assert gateway["KmsKeyArn"] == kms_key_reference

    app = cdk.App()
    workshop = synth(
        GatewayStack(
            app,
            "WorkshopGateway",
            project_name="workshoptest",
            environment="dev",
            cognito_issuer_url=issuer,
            cognito_allowed_clients=["client"],
            env=ENV,
        )
    )
    gateway = resources(workshop, "AWS::BedrockAgentCore::Gateway")[0]["Properties"]
    assert gateway["ExceptionLevel"] == "DEBUG"


# ── deployment.mode in platform.yaml is the source of truth ──


def test_production_manifest_refuses_workshop_env() -> None:
    with pytest.raises(ValueError, match="allow_mode_override"):
        resolve_deployment_mode("production", env_value="workshop")


def test_production_manifest_refuses_workshop_context() -> None:
    with pytest.raises(ValueError, match="-c deployment_mode=workshop"):
        resolve_deployment_mode("production", context_value="workshop")


def test_production_manifest_accepts_matching_values() -> None:
    assert resolve_deployment_mode("production") == "production"
    assert (
        resolve_deployment_mode(
            "production", context_value="production", env_value="production"
        )
        == "production"
    )


def test_workshop_manifest_refuses_production_env() -> None:
    with pytest.raises(ValueError, match="deployment.mode: workshop"):
        resolve_deployment_mode("workshop", env_value="production")


def test_explicit_override_wins_with_a_loud_warning(capsys) -> None:
    mode = resolve_deployment_mode(
        "production", env_value="workshop", allow_override=True
    )
    assert mode == "workshop"
    err = capsys.readouterr().err
    assert "DEPLOYMENT MODE OVERRIDDEN" in err
    assert "allow_mode_override=true" in err


def test_without_manifest_behaviour_is_unchanged() -> None:
    assert resolve_deployment_mode(None) == "workshop"
    assert resolve_deployment_mode(None, env_value="production") == "production"


def test_unknown_mode_value_is_refused() -> None:
    with pytest.raises(ValueError, match="'workshop' or 'production'"):
        resolve_deployment_mode("production", env_value="prod")
