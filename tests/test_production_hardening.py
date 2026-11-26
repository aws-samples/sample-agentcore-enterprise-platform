"""Production-only hardening controls (Bug M9/M10/M11), checked on the synthesized templates.

Each test synthesizes the production shape (retain_data=True) and, where the control is
production-only, the workshop shape too, so a control leaking into workshop or dropping out
of production fails here.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.gateway_stack import GatewayStack
from stacks.networking_stack import DEFAULT_MAX_AZS, NetworkingStack
from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")
KEY_ARN = "arn:aws:kms:us-east-1:111111111111:key/11111111-2222-3333-4444-555555555555"


def _template(stack: cdk.Stack) -> dict[str, Any]:
    return Template.from_stack(stack).to_json()


def _of_type(template: dict[str, Any], cfn_type: str) -> dict[str, dict[str, Any]]:
    return {k: r for k, r in template["Resources"].items() if r["Type"] == cfn_type}


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _networking(retain_data: bool) -> dict[str, Any]:
    return _template(
        NetworkingStack(
            cdk.App(),
            "Net",
            project_name="hard",
            environment="prod" if retain_data else "dev",
            log_retention_days=90,
            retain_data=retain_data,
            env=ENV,
        )
    )


def _observability(retain_data: bool) -> dict[str, Any]:
    return _template(
        ObservabilityStack(
            cdk.App(),
            "Obs",
            project_name="hard",
            environment="prod" if retain_data else "dev",
            monitored_resources={
                "runtime-orchestrator": "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/rt-1",
                "gateway": "arn:aws:bedrock-agentcore:us-east-1:111111111111:gateway/gw-1",
            },
            enable_traceability=True,
            enable_transaction_search=False,
            enable_alarms=True,
            log_retention_days=90,
            retain_data=retain_data,
            env=ENV,
        )
    )


def test_production_runs_one_nat_per_az_and_workshop_one():
    assert len(_of_type(_networking(True), "AWS::EC2::NatGateway")) == DEFAULT_MAX_AZS
    assert len(_of_type(_networking(False), "AWS::EC2::NatGateway")) == 1


def test_production_vpc_flow_logs_go_to_cloudwatch_with_retention():
    template = _networking(True)
    flow_logs = list(_of_type(template, "AWS::EC2::FlowLog").values())
    assert len(flow_logs) == 1
    props = flow_logs[0]["Properties"]
    assert props["TrafficType"] == "ALL"
    assert props.get("LogDestinationType", "cloud-watch-logs") == "cloud-watch-logs"
    groups = [
        g["Properties"]
        for g in _of_type(template, "AWS::Logs::LogGroup").values()
        if "flow-logs" in str(g["Properties"].get("LogGroupName", ""))
    ]
    assert groups and groups[0]["RetentionInDays"] == 90
    assert not _of_type(_networking(False), "AWS::EC2::FlowLog")


def test_production_topics_and_log_groups_use_the_observability_cmk():
    template = _observability(True)
    key_id = next(iter(_of_type(template, "AWS::KMS::Key")))
    key_ref = {"Fn::GetAtt": [key_id, "Arn"]}
    topics = list(_of_type(template, "AWS::SNS::Topic").values())
    assert len(topics) == 2  # platform alarms + security alerts
    for topic in topics:
        assert topic["Properties"]["KmsMasterKeyId"] == key_ref
    groups = [
        g["Properties"]
        for g in _of_type(template, "AWS::Logs::LogGroup").values()
        if "/aws/bedrock-agentcore/" in str(g["Properties"].get("LogGroupName", ""))
    ]
    assert len(groups) == 2  # runtime + gateway
    for group in groups:
        assert group["KmsKeyId"] == key_ref


def test_observability_key_lets_alarms_and_eventbridge_publish():
    template = _observability(True)
    key = next(iter(_of_type(template, "AWS::KMS::Key").values()))
    by_sid = {s.get("Sid"): s for s in key["Properties"]["KeyPolicy"]["Statement"]}

    alarms = by_sid["AllowAlarmPublishers"]
    assert alarms["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert alarms["Condition"]["StringEquals"]["aws:SourceAccount"] == "111111111111"

    # SNS does not support source conditions for EventBridge to an encrypted topic.
    events = by_sid["AllowEventBridgePublisher"]
    assert events["Principal"] == {"Service": "events.amazonaws.com"}
    assert "Condition" not in events
    assert set(_as_list(events["Action"])) == {"kms:Decrypt", "kms:GenerateDataKey*"}

    logs_stmt = by_sid["AllowCloudWatchLogs"]
    assert logs_stmt["Principal"] == {"Service": "logs.us-east-1.amazonaws.com"}
    assert "kms:EncryptionContext:aws:logs:arn" in logs_stmt["Condition"]["ArnLike"]


def test_workshop_observability_has_no_cmk():
    template = _observability(False)
    assert not _of_type(template, "AWS::KMS::Key")
    for topic in _of_type(template, "AWS::SNS::Topic").values():
        assert "KmsMasterKeyId" not in topic["Properties"]


def test_trail_bucket_blocks_all_public_access():
    for retain in (True, False):
        template = _template(
            SecurityStack(
                cdk.App(),
                "Sec",
                project_name="hard",
                environment="prod" if retain else "dev",
                retain_data=retain,
                env=ENV,
            )
        )
        buckets = list(_of_type(template, "AWS::S3::Bucket").values())
        assert buckets
        for bucket in buckets:
            assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }


def test_gateway_role_can_create_grants_on_its_cmk_for_aws_resources_only():
    template = _template(
        GatewayStack(
            cdk.App(),
            "Gw",
            project_name="hard",
            environment="prod",
            cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_X",
            cognito_allowed_clients=["client"],
            tool_configs={},
            kms_key_arn=KEY_ARN,
            env=ENV,
        )
    )
    grants = [
        s
        for p in _of_type(template, "AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "kms:CreateGrant" in _as_list(s["Action"])
    ]
    assert len(grants) == 1
    assert _as_list(grants[0]["Resource"]) == [KEY_ARN]
    assert grants[0]["Condition"] == {"Bool": {"kms:GrantIsForAWSResource": "true"}}
