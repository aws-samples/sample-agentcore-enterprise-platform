#!/usr/bin/env python3
"""Verify {{name}} with ONE real invocation of what it deploys.

The contract (CONTRIBUTING_USE_CASES.md): checking that a resource exists only
proves CloudFormation ran. This script proves the thing WORKS: it reads the
parameter the stack published, takes the gateway URL out of it, authenticates
with the platform M2M client the way scripts/test_gateway.py does, and asks that
gateway for its tools. Exit 0 with `OK: ...`; exit 1 with `FAIL: <cause>`; never
a traceback. `deploy.sh verify` runs this automatically when the use case is
enabled in platform.yaml.

Replace the gateway call with an invocation of whatever your stack deploys
(call the Lambda, hit the endpoint) and keep the OK/FAIL shape.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))


def main() -> int:
    # Imports live inside main so a missing dependency or a misplaced copy of
    # this file is reported as FAIL: like every other cause, not as a traceback.
    import boto3
    from test_gateway import mcp_request
    from utils import get_m2m_token

    from infra_utils.platform_config import resolve_region

    project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
    env = os.environ.get("ENVIRONMENT", "dev")
    name = f"/{project}/{env}/use-cases/{{name}}/gateway-seen"
    ssm = boto3.client("ssm", region_name=resolve_region())
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        sys.exit(f"FAIL: {name} not found — is the use case deployed?")
    _, sep, rest = value.partition("https://")
    if not sep:
        sys.exit(f"FAIL: {name} holds no gateway URL: {value!r}")
    gateway_url = sep + rest
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
