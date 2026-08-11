#!/usr/bin/env python3
"""Verify the observability claim: are traces and logs actually being accepted?

Module 9 promises per-resource logs and end-to-end traces. Checking that the
stack reached CREATE_COMPLETE cannot falsify that, and did not: the runtimes
emitted spans for weeks while X-Ray rejected every batch with HTTP 400 because
the account's trace segment destination was still XRay.

Three checks against live state:
  1. the trace segment destination is CloudWatchLogs and ACTIVE
  2. a CloudWatch Logs resource policy lets X-Ray write the span log groups
  3. vended log delivery exists for each monitored AgentCore resource

Usage:
    python scripts/check_observability.py
"""

import json
import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import get_ssm_prefix

from infra_utils.transaction_search import (
    SPAN_LOG_GROUPS,
    TRACE_DESTINATION,
)

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
PROJECT = os.environ.get("PROJECT_NAME", "agentcore-workshop")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def check_trace_destination() -> None:
    xray = boto3.client("xray", region_name=REGION)
    result = xray.get_trace_segment_destination()
    destination, status = result.get("Destination"), result.get("Status")
    if destination != TRACE_DESTINATION:
        fail(
            f"trace segment destination is {destination}, not {TRACE_DESTINATION}.\n"
            "  Every span the runtimes emit is rejected with HTTP 400 while this is\n"
            "  the case. Deploy the observability stack with\n"
            "  enable_transaction_search=true, or run:\n"
            f"    aws xray update-trace-segment-destination --destination {TRACE_DESTINATION}"
        )
    if status != "ACTIVE":
        fail(f"trace segment destination is {destination} but status is {status}")
    print(f"PASS: trace segment destination is {destination} ({status})")


def check_span_resource_policy() -> None:
    logs = boto3.client("logs", region_name=REGION)
    policies = logs.describe_resource_policies().get("resourcePolicies", [])
    for policy in policies:
        document = json.loads(policy.get("policyDocument", "{}"))
        for statement in document.get("Statement", []):
            principal = statement.get("Principal", {})
            if principal.get("Service") != "xray.amazonaws.com":
                continue
            resources = " ".join(statement.get("Resource", []))
            if all(group in resources for group in SPAN_LOG_GROUPS):
                print(
                    f"PASS: X-Ray span delivery policy present ({policy['policyName']})"
                )
                return
    fail(
        "no CloudWatch Logs resource policy allows xray.amazonaws.com to write the\n"
        f"  span log groups ({', '.join(SPAN_LOG_GROUPS)}). Transaction Search cannot\n"
        "  ingest spans without it — redeploy the observability stack."
    )


def check_log_deliveries() -> None:
    logs = boto3.client("logs", region_name=REGION)
    prefix = f"{PROJECT}-{ENVIRONMENT}"
    sources = [
        s
        for s in logs.describe_delivery_sources().get("deliverySources", [])
        if s["name"].startswith(prefix)
    ]
    if not sources:
        fail(
            f"no vended log delivery sources named {prefix}-* — the per-resource logs\n"
            "  this module promises are not being delivered."
        )
    delivered = {
        d["deliverySourceName"]
        for d in logs.describe_deliveries().get("deliveries", [])
    }
    missing = [s["name"] for s in sources if s["name"] not in delivered]
    if missing:
        fail(
            f"delivery sources exist but are not connected to a destination: {missing}"
        )
    print(f"PASS: {len(sources)} vended log deliveries active")


def main() -> None:
    print(f"Checking {get_ssm_prefix()} in {REGION}")
    check_trace_destination()
    check_span_resource_policy()
    check_log_deliveries()
    print("OK: logs and traces are being accepted")


if __name__ == "__main__":
    main()
