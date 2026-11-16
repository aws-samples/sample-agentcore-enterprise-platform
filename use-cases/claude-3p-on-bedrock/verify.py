#!/usr/bin/env python3
"""Verify claude-3p-on-bedrock with ONE real invocation of what it deploys.

The gateway is reachable only on the private network, so this script does
not call it from the operator's machine (which may be off the VPN). It reads
the probe function name the stack published, invokes that function, which runs
inside the platform VPC, and the function calls the gateway's documented
health surface through the private DNS name: /healthz, /readyz (Postgres
reachable) and /.well-known/oauth-authorization-server (200 only once config,
Entra OIDC discovery, the Bedrock upstream and Postgres migration all
succeeded), and checks the advertised issuer is the configured public URL.

Exit 0 with `OK: ...`; exit 1 with `FAIL: <cause>`; never a traceback.
`deploy.sh verify` runs this automatically when the use case is enabled; it
also runs standalone: python use-cases/claude-3p-on-bedrock/verify.py
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

USE_CASE = "claude-3p-on-bedrock"


def main() -> int:
    # Imports inside main: a missing dependency is a FAIL, not a traceback.
    import boto3
    from botocore.config import Config

    from infra_utils.platform_config import resolve_region

    project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
    env = os.environ.get("ENVIRONMENT", "dev")
    region = resolve_region()
    # Bounded so the whole script stays inside the 20 s verify budget.
    cfg = Config(connect_timeout=3, read_timeout=16, retries={"total_max_attempts": 1})
    ssm = boto3.client("ssm", region_name=region, config=cfg)
    param = f"/{project}/{env}/use-cases/{USE_CASE}/probe-function"
    try:
        fn = ssm.get_parameter(Name=param)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        sys.exit(f"FAIL: {param} not found; is the use case deployed?")

    resp = boto3.client("lambda", region_name=region, config=cfg).invoke(
        FunctionName=fn, Payload=b"{}"
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
        sys.exit(f"FAIL: probe {fn} errored: {payload.get('errorMessage', payload)}")
    if not payload.get("ok"):
        sys.exit(
            f"FAIL: gateway at {payload.get('public_url')} unhealthy: {json.dumps(payload.get('checks'))}"
        )
    meta = (
        "OAuth metadata refused to non-client address (gateway IP allow-list on)"
        if payload.get("allowlist_mode")
        else "OAuth metadata issuer matches"
    )
    print(f"OK: {payload['public_url']} healthz/readyz 200, {meta} (via in-VPC probe)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - no creds, no network, no stack: FAIL, not a trace
        sys.exit(f"FAIL: {type(e).__name__}: {e}")
