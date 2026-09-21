#!/usr/bin/env python3
"""Verify the deployment's network claim: are the agents actually in the VPC?

Three checks, all against live resources:

  1. The networking stack's private subnets sit in Availability Zones where
     AgentCore can place network interfaces. AZ *names* map to different AZ
     *ids* per account, so a VPC that works in one account fails in the next —
     with an opaque error at runtime creation time, several modules later.
  2. Every deployed runtime reports networkMode VPC with those subnets. This is
     the check that would have caught the original defect, where the VPC was
     built and every agent still ran on public networking.
  3. For a migration with private dependencies, a fixed allow-list Lambda in
     the same subnets and security group resolves each hostname and completes
     a hostname-verified TLS handshake on port 443.

Exit code 0 = the deployment matches its claim. Run it after the networking
module (module C) and after the runtimes are deployed.

Usage:
    python scripts/check_network.py            # expect VPC mode
    python scripts/check_network.py --expect-public
"""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import get_ssm_param, get_ssm_prefix

from infra_utils.platform_config import load_platform_config, resolve_region
from infra_utils.runtime_network import SUPPORTED_ZONE_IDS, unsupported_zone_ids

REGION = resolve_region()
DEPENDENCY_STATUSES = {
    "PASS",
    "DNS_FAILED",
    "CONNECT_TIMEOUT",
    "CONNECT_FAILED",
    "TLS_FAILED",
}


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def check_subnet_zones() -> list[str]:
    """Private subnets must be in AgentCore-supported AZs. Returns subnet IDs."""
    subnet_ids = [
        s for s in get_ssm_param("networking/private-subnet-ids").split(",") if s
    ]
    if not subnet_ids:
        fail("networking/private-subnet-ids is empty — redeploy the networking stack")

    ec2 = boto3.client("ec2", region_name=REGION)
    subnets = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    zone_ids = [s["AvailabilityZoneId"] for s in subnets]
    bad = unsupported_zone_ids(REGION, zone_ids)
    if bad:
        supported = " ".join(SUPPORTED_ZONE_IDS.get(REGION, ()))
        fail(
            f"subnets are in Availability Zones AgentCore does not support: {bad}.\n"
            f"  Supported in {REGION}: {supported}\n"
            "  AZ names map to different AZ ids per account, so CDK's default AZ\n"
            "  choice can land outside the supported set. Fix by pinning the VPC to\n"
            "  supported zones (aws ec2 describe-availability-zones shows the\n"
            "  mapping) and redeploying the networking stack."
        )
    print(f"PASS: private subnets are in supported AZs ({', '.join(zone_ids)})")
    return subnet_ids


def deployed_runtime_arns() -> dict[str, str]:
    """Runtime ARNs that actually exist, keyed by component.

    Enumerated with one get_parameters_by_path call rather than a lookup per
    component: utils.get_ssm_param exits the process on a miss (correct for the
    required parameters it was written for), and the A2A runtimes are optional.
    """
    ssm = boto3.client("ssm", region_name=REGION)
    found = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=f"{get_ssm_prefix()}/runtimes", Recursive=True):
        for param in page["Parameters"]:
            if param["Name"].endswith("/arn"):
                component = param["Name"].split("/runtimes/")[1].rsplit("/", 1)[0]
                found[component] = param["Value"]
    return found


def check_runtime_placement(subnet_ids: list[str], expect_public: bool) -> None:
    """Every deployed runtime must report the network mode we claim."""
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    runtimes = deployed_runtime_arns()
    checked = 0
    for component, arn in sorted(runtimes.items()):
        runtime = control.get_agent_runtime(agentRuntimeId=arn.rsplit("/", 1)[-1])
        network = runtime.get("networkConfiguration", {})
        mode = network.get("networkMode")
        checked += 1

        if expect_public:
            if mode != "PUBLIC":
                fail(f"{component}: expected networkMode PUBLIC, got {mode}")
            print(f"PASS: {component} is PUBLIC")
            continue

        if mode != "VPC":
            fail(
                f"{component}: networkMode is {mode}, not VPC — the VPC exists but the\n"
                "  agent is not in it. Redeploy the runtime with enable_networking=true."
            )
        placed = set(network.get("networkModeConfig", {}).get("subnets", []))
        missing = set(subnet_ids) - placed
        if missing:
            fail(
                f"{component}: VPC mode but not in the expected subnets: {sorted(missing)}"
            )
        print(f"PASS: {component} is in the VPC ({len(placed)} subnets)")

    if not checked:
        fail("no runtimes found in SSM — deploy a runtime stack first")


def configured_private_dependencies() -> list[str]:
    value = os.environ.get("MIGRATION_PRIVATE_DEPENDENCIES")
    if value is not None:
        return [host.strip() for host in value.split(",") if host.strip()]

    manifest = Path(
        os.environ.get(
            "PLATFORM_CONFIG", Path(__file__).resolve().parents[1] / "platform.yaml"
        )
    )
    if not manifest.exists():
        return []
    try:
        config = load_platform_config(manifest)
    except Exception:  # noqa: BLE001 — validation reports detail in deploy/design
        fail("cannot validate private dependencies because platform.yaml is invalid")
    if config.migration is None:
        return []
    return config.migration.network.private_dependencies


def check_private_dependencies() -> None:
    """Invoke the fixed allow-list probe from the runtime VPC."""
    expected = configured_private_dependencies()
    if not expected:
        return

    parameter_name = f"{get_ssm_prefix()}/networking/migration-dependency-probe-name"
    ssm = boto3.client("ssm", region_name=REGION)
    try:
        function_name = ssm.get_parameter(Name=parameter_name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            fail(
                "private dependencies are configured but the network probe is "
                "missing — redeploy the networking stack"
            )
        fail("could not read the migration network-probe parameter")

    try:
        response = boto3.client("lambda", region_name=REGION).invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=b"{}",
        )
    except ClientError:
        fail("could not invoke the migration dependency probe")
    if response.get("FunctionError"):
        fail("migration dependency probe failed; inspect its protected Lambda logs")
    try:
        payload = json.loads(response["Payload"].read())
    except (KeyError, TypeError, ValueError):
        fail("migration dependency probe returned an invalid response")

    if payload.get("configurationError"):
        fail(
            "migration dependency probe could not load the declared CA bundle; "
            "check its secret and IAM policy"
        )
    results = payload.get("results")
    if not isinstance(results, list):
        fail("migration dependency probe returned no result list")
    by_host = {
        item.get("host"): item.get("status")
        for item in results
        if isinstance(item, dict)
    }
    if set(by_host) != set(expected):
        fail("migration dependency probe result does not match the manifest allow-list")

    failed = False
    for host in expected:
        status = by_host[host]
        safe_status = status if status in DEPENDENCY_STATUSES else "UNKNOWN"
        if safe_status == "PASS":
            print(f"PASS: {host} resolves and accepts a trusted TLS connection")
        else:
            failed = True
            print(f"FAIL: {host}: {safe_status}", file=sys.stderr)
    if failed or payload.get("ok") is not True:
        fail("one or more migration private dependencies are unreachable")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify runtime network placement")
    parser.add_argument(
        "--expect-public",
        action="store_true",
        help="Assert runtimes are PUBLIC (for deployments without enable_networking)",
    )
    args = parser.parse_args()
    print(f"Checking {get_ssm_prefix()} in {REGION}")

    if args.expect_public:
        check_runtime_placement([], expect_public=True)
    else:
        check_runtime_placement(check_subnet_zones(), expect_public=False)
        check_private_dependencies()
    print("OK: network placement matches the deployment's claim")


if __name__ == "__main__":
    main()
