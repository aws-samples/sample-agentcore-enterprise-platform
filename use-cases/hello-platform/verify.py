#!/usr/bin/env python3
"""Verify hello-platform with ONE real invocation of what it deploys.

The use case publishes one SSM parameter carrying the gateway URL it
discovered through the platform interface. Checking that the parameter
exists only proves CloudFormation ran; this script proves the value is
USABLE: it takes the URL out of the parameter, authenticates with the
platform's M2M client exactly like scripts/test_gateway.py, and asks that
gateway for its tool list. Exits 0 with `OK: ...`, 1 with `FAIL: <cause>`,
never with a stack trace — a verify that cannot fail is not a verify.

Usage: python use-cases/hello-platform/verify.py
Respects PROJECT_NAME / ENVIRONMENT / AWS_REGION; `deploy.sh verify` runs it
automatically when the use case is enabled in platform.yaml.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))  # utils + test_gateway helpers

import boto3
from test_gateway import mcp_request
from utils import get_m2m_token

from infra_utils.platform_config import resolve_region


def main() -> int:
    project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
    env = os.environ.get("ENVIRONMENT", "dev")
    name = f"/{project}/{env}/use-cases/hello-platform/gateway-seen"

    ssm = boto3.client("ssm", region_name=resolve_region())
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        sys.exit(f"FAIL: {name} not found — is the use case deployed?")

    # stack.py writes "<greeting>: <gateway_url>"; everything from https:// on
    # is the URL the use case claims to have discovered.
    _, sep, gateway_url = value.partition("https://")
    if not sep:
        sys.exit(f"FAIL: {name} holds no gateway URL: {value!r}")
    gateway_url = sep + gateway_url

    token = get_m2m_token(project, env)
    result = mcp_request(gateway_url, token, "tools/list")
    tools = result.get("result", {}).get("tools")
    if not isinstance(tools, list) or not tools:
        sys.exit(f"FAIL: {gateway_url} answered tools/list without tools: {result}")

    print(f"OK: {name} -> {gateway_url} lists {len(tools)} tool(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — no creds, no network, no stack: FAIL, not a trace
        sys.exit(f"FAIL: {type(e).__name__}: {e}")
