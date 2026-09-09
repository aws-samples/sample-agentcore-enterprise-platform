#!/usr/bin/env python3
"""Verify the observability.alarms claim: do the alarms exist, is any firing?

Two failure modes stack status cannot catch:

  1. Drift — the flag is on but no alarms named {project}-{environment}-*
     exist (deployed before the flag, or deleted in the console).
  2. An alarm in ALARM state — the platform is unhealthy at verification
     time, and a verification run should say so instead of "all green".

INSUFFICIENT_DATA passes: the error metrics only emit when something fails,
so a healthy idle platform never feeds them.

Usage:
    python scripts/check_alarms.py
"""

import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import DEFAULT_ENV, DEFAULT_PROJECT

from infra_utils.platform_config import resolve_region

REGION = resolve_region()


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    project = os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
    environment = os.environ.get("ENVIRONMENT", DEFAULT_ENV)
    prefix = f"{project}-{environment}-"
    print(f"Checking alarms named {prefix}* in {REGION}")

    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    alarms = []
    for page in cloudwatch.get_paginator("describe_alarms").paginate(
        AlarmNamePrefix=prefix
    ):
        alarms.extend(page["MetricAlarms"])

    if not alarms:
        fail(
            f"no CloudWatch alarms named {prefix}* — observability.alarms is "
            "on but none are deployed. Redeploy the observability stack."
        )

    firing = []
    for alarm in sorted(alarms, key=lambda a: a["AlarmName"]):
        print(f"  {alarm['AlarmName']}: {alarm['StateValue']}")
        if alarm["StateValue"] == "ALARM":
            firing.append(alarm["AlarmName"])

    if firing:
        fail(
            f"{len(firing)} alarm(s) in ALARM state: {', '.join(firing)} — "
            "the platform is unhealthy right now. Each alarm's description "
            "says what to check first."
        )
    print(f"OK: {len(alarms)} alarms present, none firing")


if __name__ == "__main__":
    main()
