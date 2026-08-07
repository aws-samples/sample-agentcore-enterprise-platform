"""CloudWatch Transaction Search prerequisites for AgentCore tracing.

Deliberately free of CDK imports so it stays unit-testable without
aws-cdk-lib (see tests/test_transaction_search.py).

Without Transaction Search the runtimes still emit OTLP spans and X-Ray
rejects every batch with HTTP 400 ("The OTLP API is supported with CloudWatch
Logs as a Trace Segment Destination"), so a deployment can advertise
end-to-end tracing and deliver none. Two things have to be true, per
"Enabling CloudWatch Transaction Search":

  1. a CloudWatch Logs resource policy letting X-Ray write spans, and
  2. the account's trace segment destination set to CloudWatchLogs.

Both are account- and region-scoped, not per-resource — see the caveat in the
observability stack.
"""

# Log groups Transaction Search writes to. aws/spans holds the spans
# themselves; the application-signals group holds the derived service data.
SPAN_LOG_GROUPS = ("aws/spans", "/aws/application-signals/data")

TRACE_DESTINATION = "CloudWatchLogs"


def transaction_search_caller_statements(partition: str = "aws") -> list[dict]:
    """IAM statements the principal enabling Transaction Search needs.

    UpdateTraceSegmentDestination does considerably more than its name says: it
    provisions the span log groups, checks the logs resource policy, and kicks
    off Application Signals discovery (which creates a service-linked role and a
    CloudTrail service-linked channel). Each missing permission surfaces as a
    separate AccessDenied one deploy at a time, so the full set is kept here,
    from "Enable transaction search" > Prerequisites.
    """
    app_signals_slr = (
        f"arn:{partition}:iam::*:role/aws-service-role/"
        "application-signals.cloudwatch.amazonaws.com/"
        "AWSServiceRoleForCloudWatchApplicationSignals"
    )
    return [
        {
            "Sid": "TransactionSearchXRayPermissions",
            "Effect": "Allow",
            "Action": [
                "xray:GetTraceSegmentDestination",
                "xray:UpdateTraceSegmentDestination",
                "xray:GetIndexingRules",
                "xray:UpdateIndexingRule",
            ],
            "Resource": "*",
        },
        {
            "Sid": "TransactionSearchLogGroupPermissions",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutRetentionPolicy",
            ],
            "Resource": [
                f"arn:{partition}:logs:*:*:log-group:{group}:*"
                for group in SPAN_LOG_GROUPS
            ],
        },
        {
            "Sid": "TransactionSearchLogsPermissions",
            "Effect": "Allow",
            "Action": ["logs:PutResourcePolicy", "logs:DescribeResourcePolicies"],
            "Resource": "*",
        },
        {
            "Sid": "TransactionSearchApplicationSignalsPermissions",
            "Effect": "Allow",
            "Action": ["application-signals:StartDiscovery"],
            "Resource": "*",
        },
        {
            "Sid": "CloudWatchApplicationSignalsCreateServiceLinkedRolePermissions",
            "Effect": "Allow",
            "Action": "iam:CreateServiceLinkedRole",
            "Resource": app_signals_slr,
            "Condition": {
                "StringLike": {
                    "iam:AWSServiceName": "application-signals.cloudwatch.amazonaws.com"
                }
            },
        },
        {
            "Sid": "CloudWatchApplicationSignalsGetRolePermissions",
            "Effect": "Allow",
            "Action": "iam:GetRole",
            "Resource": app_signals_slr,
        },
        {
            "Sid": "CloudWatchApplicationSignalsCloudTrailPermissions",
            "Effect": "Allow",
            "Action": ["cloudtrail:CreateServiceLinkedChannel"],
            "Resource": (
                f"arn:{partition}:cloudtrail:*:*:channel/aws-service-channel/"
                "application-signals/*"
            ),
        },
    ]


def xray_logs_resource_policy(
    account_id: str, region: str, partition: str = "aws"
) -> dict:
    """Resource policy allowing X-Ray to deliver spans into CloudWatch Logs.

    The SourceArn/SourceAccount conditions are what stop this from being a
    confused-deputy hole: only X-Ray acting for THIS account may write.
    """
    resources = [
        f"arn:{partition}:logs:{region}:{account_id}:log-group:{group}:*"
        for group in SPAN_LOG_GROUPS
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": resources,
                "Condition": {
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{partition}:xray:{region}:{account_id}:*"
                    },
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }
