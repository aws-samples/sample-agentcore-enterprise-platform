"""Workshop Monitor — polls AWS for deployment status and writes JSON for the dashboard.

Scope comes from the deployment contract: expected_stacks() says which stacks
THIS configuration promises, so a stack the config does not ask for is
reported as "not applicable" rather than missing or failed. A healthy
greenfield deployment therefore reads 6/6, not 6/10.

Status classification lives here and ONLY here: each stack carries a `state`
field, and the browser renders that instead of re-deriving it. The two used to
disagree — this file counted ROLLBACK_COMPLETE as both deployed ("COMPLETE" is
a substring) and failed ("ROLLBACK" is too), which could drive not_deployed
negative, while the browser called it failed.
"""

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra_utils.platform_config import (
    PlatformConfig,
    load_platform_config,
)

logger = logging.getLogger("monitor")

PROJECT = os.environ.get("PROJECT_NAME", "agentcore-workshop")
ENV = os.environ.get("ENVIRONMENT", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = f"{PROJECT}-{ENV}"
OUTPUT = os.path.join(os.path.dirname(__file__), "public", "status.json")

# Presentation metadata per stack suffix. Module ids match deploy.sh's
# MODULE_MAP (tests/test_dashboard.py fails if they drift).
STACK_META = {
    "networking": {
        "module": "C",
        "team": "platform",
        "layer": "foundation",
        "description": "VPC + subnets + VPC endpoints (optional)",
    },
    "security": {
        "module": "E",
        "team": "security",
        "layer": "foundation",
        "description": "KMS CMK + CloudTrail (optional)",
    },
    "auth": {
        "module": "3",
        "team": "platform",
        "layer": "identity",
        "description": "Cognito User Pool + federated IdP + 3 OAuth clients",
    },
    "identity": {
        "module": "4",
        "team": "platform",
        "layer": "identity",
        "description": "OAuth2 credential providers (gateway M2M + optional 3LO)",
    },
    "gateway": {
        "module": "5",
        "team": "platform",
        "layer": "service",
        "description": "MCP Gateway + CUSTOM_JWT auth + tool targets",
    },
    "memory": {
        "module": "A",
        "team": "agent",
        "layer": "service",
        "description": "AgentCore Memory + optional long-term strategy",
    },
    "runtime-orchestrator": {
        "module": "6",
        "team": "agent",
        "layer": "runtime",
        "description": "Orchestrator agent (HTTP) — ECR + CodeBuild + CfnRuntime",
    },
    "runtime-code-agent": {
        "module": "8",
        "team": "agent",
        "layer": "runtime",
        "description": "Code Agent (A2A protocol)",
    },
    "runtime-research-agent": {
        "module": "8",
        "team": "agent",
        "layer": "runtime",
        "description": "Research Agent (A2A protocol)",
    },
    "observability": {
        "module": "9",
        "team": "platform",
        "layer": "observability",
        "description": "CloudWatch vended logs + X-Ray per resource",
    },
}

cfn = boto3.client("cloudformation", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)


def classify(status: str) -> str:
    """CloudFormation status → one of deployed / in-progress / failed /
    not-deployed / not-applicable. Substring order matters: NOT_DEPLOYED
    contains "deployed" and ROLLBACK_COMPLETE contains "complete", so the
    negative states are tested first."""
    s = (status or "").lower().replace("_", "-")
    if not s or s == "not-deployed" or "delete-complete" in s:
        return "not-deployed"
    if s == "not-applicable":
        return "not-applicable"
    if "fail" in s or "rollback" in s or "error" in s:
        return "failed"
    if "progress" in s or "pending" in s:
        return "in-progress"
    if "complete" in s:
        return "deployed"
    return "not-deployed"


def load_config() -> PlatformConfig:
    path = os.environ.get("PLATFORM_CONFIG", "platform.yaml")
    try:
        return load_platform_config(path) if os.path.exists(path) else PlatformConfig()
    except Exception as exc:  # noqa: BLE001 — a bad manifest must not blind the dashboard
        logger.warning("platform.yaml not usable (%s); assuming defaults", exc)
        return PlatformConfig()


def caller_account() -> str:
    try:
        return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 — only federation needs the account
        logger.debug("no caller identity yet: %s", exc)
        return ""


def expected_suffixes(config: PlatformConfig, account: str) -> list[str]:
    """Stack suffixes this configuration promises, per the contract."""
    prefix = f"{config.project}-{config.environment}-"
    try:
        return [s.removeprefix(prefix) for s in config.expected_stacks(account)]
    except Exception as exc:  # noqa: BLE001 — e.g. a federated file from an unlisted account
        logger.warning("contract could not resolve a footprint (%s)", exc)
        return list(STACK_META)


def deployment_view(config: PlatformConfig, account: str) -> dict:
    """Which accounts this deployment spans, and which one is being polled.

    A dashboard only ever sees ONE account. In a federated deployment the
    other side's stacks are not observable from here, so the architecture
    graph draws them as "not observed" rather than inventing a status.
    """
    dep = config.deployment
    role = None
    try:
        role = config.federated_role(account)
    except Exception as exc:  # noqa: BLE001 — account in neither list; app.py reports that
        logger.debug("federated role undetermined: %s", exc)
    return {
        "strategy": dep.strategy,
        "role": role,  # platform | workload | None (centralized/distributed)
        "polled_account": account,
        "platform_account": dep.platform_account,
        "workload_accounts": list(dep.workload_accounts),
    }


def get_stack_status(stack_name: str) -> dict:
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        resources = []
        try:
            paginator = cfn.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=stack_name):
                resources.extend(
                    {
                        "type": r["ResourceType"],
                        "logical": r["LogicalResourceId"],
                        "status": r["ResourceStatus"],
                        "physical": r.get("PhysicalResourceId", ""),
                    }
                    for r in page["StackResourceSummaries"]
                )
        except Exception as exc:  # noqa: BLE001 — a stack mid-create lists nothing yet
            logger.debug("no resources for %s: %s", stack_name, exc)
        return {
            "status": stack["StackStatus"],
            "outputs": {
                o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])
            },
            "resources": resources,
        }
    except cfn.exceptions.ClientError:
        return {"status": "NOT_DEPLOYED", "outputs": {}, "resources": []}


def get_ssm_params() -> dict:
    params = {}
    try:
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=f"/{PROJECT}/{ENV}", Recursive=True):
            for p in page["Parameters"]:
                params[p["Name"].replace(f"/{PROJECT}/{ENV}/", "")] = p["Value"]
    except Exception as exc:  # noqa: BLE001 — none published before the first deploy
        logger.debug("no SSM parameters yet: %s", exc)
    return params


def poll() -> dict:
    config = load_config()
    account = caller_account()
    in_scope = expected_suffixes(config, account)
    stacks_status = {}
    for suffix, meta in STACK_META.items():
        name = f"{PREFIX}-{suffix}"
        if suffix in in_scope:
            info = get_stack_status(name)
        else:
            # Out of scope for this configuration: not missing, not failed.
            info = {"status": "NOT_APPLICABLE", "outputs": {}, "resources": []}
        stacks_status[name] = {**meta, **info, "state": classify(info["status"])}

    # Counted from the emitted states, never by subtraction — the old
    # arithmetic could report a negative not_deployed.
    states = [s["state"] for s in stacks_status.values()]
    return {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "project": PROJECT,
        "environment": ENV,
        "region": REGION,
        "account": account,
        "deployment": deployment_view(config, account),
        "expected_stacks": [f"{PREFIX}-{s}" for s in in_scope],
        "summary": {
            "total_stacks": len(in_scope),
            "deployed": states.count("deployed"),
            "in_progress": states.count("in-progress"),
            "failed": states.count("failed"),
            "not_deployed": states.count("not-deployed"),
            "not_applicable": states.count("not-applicable"),
            "total_resources": sum(len(s["resources"]) for s in stacks_status.values()),
        },
        "stacks": stacks_status,
        "ssm_parameters": get_ssm_params(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    print(f"Monitoring {PREFIX} in {REGION}... Writing to {OUTPUT}")
    while True:
        try:
            data = poll()
            with open(OUTPUT, "w") as f:
                json.dump(data, f, indent=2, default=str)
            d = data["summary"]
            print(
                f"[{data['timestamp']}] Deployed: {d['deployed']}/{d['total_stacks']} | "
                f"In Progress: {d['in_progress']} | Failed: {d['failed']} | "
                f"N/A: {d['not_applicable']} | Resources: {d['total_resources']}"
            )
        except Exception as exc:  # noqa: BLE001 — a poll loop must survive any single failure
            logger.error("poll failed: %s", exc)
        time.sleep(15)
