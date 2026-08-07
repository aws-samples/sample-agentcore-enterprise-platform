"""Transaction Search prerequisites must be exact, or tracing silently fails.

Regression guard: module 9 advertised "end-to-end request traces" while the
account's trace segment destination was still XRay, so every span batch the
runtimes emitted came back as HTTP 400 and the module's verify — a check that
the stack reached COMPLETE — passed anyway.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra_utils.transaction_search import (
    SPAN_LOG_GROUPS,
    TRACE_DESTINATION,
    transaction_search_caller_statements,
    xray_logs_resource_policy,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"


def _statement(policy: dict) -> dict:
    assert len(policy["Statement"]) == 1
    return policy["Statement"][0]


def test_policy_allows_only_xray_to_put_span_events():
    statement = _statement(xray_logs_resource_policy(ACCOUNT, REGION))
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "xray.amazonaws.com"}
    assert statement["Action"] == "logs:PutLogEvents"


def test_policy_covers_both_span_log_groups():
    resources = _statement(xray_logs_resource_policy(ACCOUNT, REGION))["Resource"]
    assert len(resources) == len(SPAN_LOG_GROUPS)
    for group in SPAN_LOG_GROUPS:
        assert any(f":log-group:{group}:*" in r for r in resources), group


def test_policy_is_scoped_to_this_account():
    """Without both conditions this is a confused-deputy hole."""
    condition = _statement(xray_logs_resource_policy(ACCOUNT, REGION))["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT
    source_arn = condition["ArnLike"]["aws:SourceArn"]
    assert source_arn == f"arn:aws:xray:{REGION}:{ACCOUNT}:*"


def test_policy_follows_region_and_partition():
    policy = xray_logs_resource_policy(ACCOUNT, "eu-west-1", partition="aws-cn")
    statement = _statement(policy)
    assert all(
        r.startswith("arn:aws-cn:logs:eu-west-1:") for r in statement["Resource"]
    )
    assert (
        "arn:aws-cn:xray:eu-west-1:"
        in statement["Condition"]["ArnLike"]["aws:SourceArn"]
    )


def test_destination_is_the_value_the_otlp_api_requires():
    """'CloudWatchLogs' exactly — 'XRay' is what makes span export return 400."""
    assert TRACE_DESTINATION == "CloudWatchLogs"


def test_caller_statements_cover_every_hidden_dependency():
    """Each of these was a separate AccessDenied, one failed deploy apiece."""
    actions = set()
    for statement in transaction_search_caller_statements():
        action = statement["Action"]
        actions.update([action] if isinstance(action, str) else action)

    required = {
        "xray:UpdateTraceSegmentDestination",
        "xray:GetTraceSegmentDestination",
        "logs:PutRetentionPolicy",  # provisions the span log groups
        "logs:PutResourcePolicy",
        "application-signals:StartDiscovery",  # discovery on enable
        "iam:CreateServiceLinkedRole",  # app-signals SLR
        "cloudtrail:CreateServiceLinkedChannel",
    }
    assert required <= actions, f"missing: {sorted(required - actions)}"


def test_caller_log_group_access_is_scoped_to_span_groups():
    """Log-group writes must not be account-wide."""
    for statement in transaction_search_caller_statements():
        if statement["Sid"] != "TransactionSearchLogGroupPermissions":
            continue
        resources = statement["Resource"]
        assert "*" not in resources, "log group access must not be unscoped"
        assert len(resources) == len(SPAN_LOG_GROUPS)
        return
    raise AssertionError("log group statement missing")


def test_service_linked_role_grant_is_pinned_to_one_service():
    """iam:CreateServiceLinkedRole without the condition is a privilege hole."""
    for statement in transaction_search_caller_statements():
        if statement["Action"] != "iam:CreateServiceLinkedRole":
            continue
        service = statement["Condition"]["StringLike"]["iam:AWSServiceName"]
        assert service == "application-signals.cloudwatch.amazonaws.com"
        assert statement["Resource"].endswith(
            "AWSServiceRoleForCloudWatchApplicationSignals"
        )
        return
    raise AssertionError("service-linked role statement missing")


def test_caller_statements_follow_the_partition():
    statements = transaction_search_caller_statements(partition="aws-us-gov")
    arns = [
        r
        for s in statements
        for r in ([s["Resource"]] if isinstance(s["Resource"], str) else s["Resource"])
        if r != "*"
    ]
    assert arns and all(a.startswith("arn:aws-us-gov:") for a in arns)
