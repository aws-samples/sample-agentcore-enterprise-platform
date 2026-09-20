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
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra_utils.platform_config import (
    PlatformConfig,
    load_platform_config,
    resolve_region,
)

logger = logging.getLogger("monitor")

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

# Only these CloudFormation outputs are public dashboard data. CDK creates
# additional Outputs for cross-stack references, and those can contain values
# such as generated client secrets. New outputs stay private until reviewed and
# deliberately added here.
SAFE_STACK_OUTPUTS = {
    "networking": frozenset({"VpcId", "PrivateSubnetIds", "RuntimeSecurityGroupId"}),
    "security": frozenset({"KmsKeyArn"}),
    "auth": frozenset(
        {
            "UserPoolId",
            "UserPoolArn",
            "IssuerUrl",
            "DiscoveryUrl",
            "AppClientId",
            "WebClientId",
            "M2MClientId",
            "DomainUrl",
            "IdPType",
            "IdPMode",
        }
    ),
    "identity": frozenset(
        {
            "GatewayCredentialProviderName",
            "GoogleProviderArn",
            "GithubProviderArn",
            "NotionProviderArn",
        }
    ),
    "gateway": frozenset({"GatewayUrl", "GatewayArn", "GatewayId"}),
    "memory": frozenset({"MemoryId", "MemoryArn"}),
    "runtime-orchestrator": frozenset(
        {"RuntimeArn", "RuntimeId", "SourceHash", "ImageUri"}
    ),
    "runtime-code-agent": frozenset(
        {"RuntimeArn", "RuntimeId", "SourceHash", "ImageUri"}
    ),
    "runtime-research-agent": frozenset(
        {"RuntimeArn", "RuntimeId", "SourceHash", "ImageUri"}
    ),
    "observability": frozenset({"SecurityAlertsTopicArn", "MonitoredResources"}),
}

# Workshop handoff exports need two additional, non-secret references during
# a federated legacy-client rotation. Keep them out of the browser-facing
# dashboard while allowing the export artifact to identify the replacement
# client and the Secrets Manager name from which an approved operator retrieves
# its value.
SAFE_EXPORT_STACK_OUTPUTS = {
    **SAFE_STACK_OUTPUTS,
    "auth": SAFE_STACK_OUTPUTS["auth"]
    | frozenset({"M2MClientIdV2Export", "M2MClientSecretNameV2Export"}),
}

# SSM is queried recursively, so the namespace can contain customer-created
# parameters as well as this platform's public interface. Keep only the
# reviewed, non-secret interface fields the dashboard actually presents.
SAFE_SSM_PARAMETERS = frozenset(
    {
        "auth/mode",
        "auth/issuer-url",
        "auth/user-pool-id",
        "auth/app-client-id",
        "auth/web-client-id",
        "auth/m2m-client-id",
        "auth/m2m-scope",
        "gateway/url",
        "identity/gateway-credential-provider-name",
        "identity/google-provider-arn",
        "identity/github-provider-arn",
        "identity/notion-provider-arn",
        "memory/memory-id",
        "memory/memory-arn",
        "networking/vpc-id",
        "networking/private-subnet-ids",
        "networking/runtime-security-group-id",
        "runtimes/orchestrator/arn",
        "runtimes/orchestrator/id",
        "runtimes/code-agent/arn",
        "runtimes/code-agent/id",
        "runtimes/research-agent/arn",
        "runtimes/research-agent/id",
    }
)


@dataclass(frozen=True)
class PollContext:
    config: PlatformConfig
    project: str
    environment: str
    region: str
    prefix: str


class PollError(RuntimeError):
    """A safe, user-actionable AWS polling error for status.json."""

    def __init__(self, service: str, code: str, message: str):
        super().__init__(message)
        self.service = service
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "code": self.code,
            "message": self.message,
        }


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
    except Exception as exc:
        logger.error("platform configuration is invalid: %s", exc)
        raise PollError(
            "Configuration",
            "InvalidConfiguration",
            f"{path} is invalid. Run './scripts/deploy.sh design' and fix the "
            "reported configuration errors.",
        ) from exc


def runtime_context() -> PollContext:
    """Resolve naming and region for this poll, after loading platform.yaml.

    Environment variables retain the same precedence as deploy.sh. Resolving
    this on every poll means a restarted credential/profile or edited manifest
    cannot leave the dashboard querying clients created for an old region.
    """
    config = load_config()
    project = os.environ.get("PROJECT_NAME") or config.project
    environment = os.environ.get("ENVIRONMENT") or config.environment
    region = resolve_region()
    return PollContext(
        config=config,
        project=project,
        environment=environment,
        region=region,
        prefix=f"{project}-{environment}",
    )


def _poll_error(service: str, region: str, exc: Exception) -> PollError:
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return PollError(
            service,
            "CredentialsUnavailable",
            "AWS credentials are missing or incomplete. Refresh the configured "
            "profile and retry.",
        )
    if isinstance(
        exc,
        (
            EndpointConnectionError,
            ConnectionClosedError,
            ConnectTimeoutError,
            ReadTimeoutError,
        ),
    ):
        return PollError(
            service,
            "EndpointUnavailable",
            f"Could not reach the {service} endpoint in {region}. Check network "
            "connectivity and the configured region.",
        )
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "ClientError"))
        if code in {
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedOperation",
        }:
            message = (
                f"AWS denied the dashboard permission to read {service}. "
                "Check the active role and its read-only permissions."
            )
        elif code in {
            "ExpiredToken",
            "ExpiredTokenException",
            "InvalidClientTokenId",
            "UnrecognizedClientException",
        }:
            message = (
                "AWS credentials are invalid or expired. Refresh the configured "
                "profile and retry."
            )
        else:
            message = (
                f"{service} returned {code} while the dashboard was polling. "
                "Check the active account, region, and AWS service status."
            )
        return PollError(service, code, message)
    if isinstance(exc, BotoCoreError):
        return PollError(
            service,
            type(exc).__name__,
            f"The AWS SDK could not poll {service}. Check the active profile, "
            "region, and network connectivity.",
        )
    return PollError(
        service,
        type(exc).__name__,
        f"The dashboard could not poll {service}. Check the monitor logs.",
    )


def caller_account(sts_client, region: str) -> str:
    try:
        return sts_client.get_caller_identity()["Account"]
    except Exception as exc:
        raise _poll_error("STS", region, exc) from exc


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


def _stack_not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    return (
        error.get("Code") == "ValidationError"
        and "exist" in str(error.get("Message", "")).lower()
    )


def safe_stack_outputs(
    stack_suffix: str,
    outputs: list[dict],
    *,
    allowlist: dict[str, frozenset[str]] = SAFE_STACK_OUTPUTS,
) -> dict[str, str]:
    """Return reviewed display fields only; unknown outputs never serialize."""
    allowed = allowlist.get(stack_suffix, frozenset())
    return {
        output["OutputKey"]: output["OutputValue"]
        for output in outputs
        if output.get("OutputKey") in allowed and "OutputValue" in output
    }


def get_stack_status(
    cfn_client, stack_name: str, stack_suffix: str, region: str
) -> dict:
    try:
        stack = cfn_client.describe_stacks(StackName=stack_name)["Stacks"][0]
        resources = []
        paginator = cfn_client.get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=stack_name):
            resources.extend(
                {
                    "type": r["ResourceType"],
                    "logical": r["LogicalResourceId"],
                    "status": r["ResourceStatus"],
                }
                for r in page["StackResourceSummaries"]
            )
        return {
            "status": stack["StackStatus"],
            "outputs": safe_stack_outputs(stack_suffix, stack.get("Outputs", [])),
            "resources": resources,
        }
    except PollError:
        raise
    except ClientError as exc:
        if _stack_not_found(exc):
            return {"status": "NOT_DEPLOYED", "outputs": {}, "resources": []}
        raise _poll_error("CloudFormation", region, exc) from exc
    except Exception as exc:
        raise _poll_error("CloudFormation", region, exc) from exc


def get_ssm_params(ssm_client, project: str, environment: str, region: str) -> dict:
    params = {}
    root = f"/{project}/{environment}"
    try:
        paginator = ssm_client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(
            Path=root,
            Recursive=True,
            WithDecryption=False,
        ):
            for p in page["Parameters"]:
                name = p["Name"].removeprefix(f"{root}/")
                if name in SAFE_SSM_PARAMETERS:
                    params[name] = p["Value"]
    except Exception as exc:
        raise _poll_error("SSM", region, exc) from exc
    return params


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _error_status(context: PollContext, error: PollError) -> dict:
    return {
        "timestamp": _timestamp(),
        "available": False,
        "poll_error": error.as_dict(),
        "project": context.project,
        "environment": context.environment,
        "region": context.region,
        "account": "",
        "deployment": {
            "strategy": context.config.deployment.strategy,
            "role": None,
            "polled_account": "",
            "platform_account": context.config.deployment.platform_account,
            "workload_accounts": list(context.config.deployment.workload_accounts),
        },
        "expected_stacks": [],
        "summary": {
            "total_stacks": 0,
            "deployed": 0,
            "in_progress": 0,
            "failed": 0,
            "not_deployed": 0,
            "not_applicable": 0,
            "total_resources": 0,
        },
        "stacks": {},
        "ssm_parameters": {},
    }


def _unavailable_context() -> PollContext:
    """Minimal display context when the configured manifest cannot be loaded."""
    fallback = PlatformConfig()
    project = os.environ.get("PROJECT_NAME") or fallback.project
    environment = os.environ.get("ENVIRONMENT") or fallback.environment
    return PollContext(
        config=fallback,
        project=project,
        environment=environment,
        region=(
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "unknown"
        ),
        prefix=f"{project}-{environment}",
    )


def poll(client_factory=boto3.client) -> dict:
    context = None
    try:
        context = runtime_context()
        cfn_client = client_factory("cloudformation", region_name=context.region)
        ssm_client = client_factory("ssm", region_name=context.region)
        sts_client = client_factory("sts", region_name=context.region)
        account = caller_account(sts_client, context.region)
        in_scope = expected_suffixes(context.config, account)
        stacks_status = {}
        for suffix, meta in STACK_META.items():
            name = f"{context.prefix}-{suffix}"
            if suffix in in_scope:
                info = get_stack_status(
                    cfn_client,
                    name,
                    suffix,
                    context.region,
                )
            else:
                # Out of scope for this configuration: not missing, not failed.
                info = {"status": "NOT_APPLICABLE", "outputs": {}, "resources": []}
            stacks_status[name] = {
                **meta,
                **info,
                "state": classify(info["status"]),
            }

        # Counted from the emitted states, never by subtraction — the old
        # arithmetic could report a negative not_deployed.
        states = [s["state"] for s in stacks_status.values()]
        return {
            "timestamp": _timestamp(),
            "available": True,
            "poll_error": None,
            "project": context.project,
            "environment": context.environment,
            "region": context.region,
            "account": account,
            "deployment": deployment_view(context.config, account),
            "expected_stacks": [f"{context.prefix}-{s}" for s in in_scope],
            "summary": {
                "total_stacks": len(in_scope),
                "deployed": states.count("deployed"),
                "in_progress": states.count("in-progress"),
                "failed": states.count("failed"),
                "not_deployed": states.count("not-deployed"),
                "not_applicable": states.count("not-applicable"),
                "total_resources": sum(
                    len(s["resources"]) for s in stacks_status.values()
                ),
            },
            "stacks": stacks_status,
            "ssm_parameters": get_ssm_params(
                ssm_client,
                context.project,
                context.environment,
                context.region,
            ),
        }
    except PollError as exc:
        context = context or _unavailable_context()
        logger.error("poll failed: %s", exc.message)
        return _error_status(context, exc)
    except Exception as exc:
        context = context or _unavailable_context()
        error = _poll_error("AWS", context.region, exc)
        logger.exception("poll failed unexpectedly")
        return _error_status(context, error)


def write_status(path: str | os.PathLike[str], data: dict) -> None:
    """Atomically replace status.json with a file readable only by its owner."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    print(f"Monitoring platform deployment... Writing to {OUTPUT}")
    while True:
        try:
            data = poll()
            write_status(OUTPUT, data)
            d = data["summary"]
            if data["poll_error"]:
                print(
                    f"[{data['timestamp']}] Poll unavailable: "
                    f"{data['poll_error']['message']}"
                )
            else:
                print(
                    f"[{data['timestamp']}] {data['project']}-{data['environment']} "
                    f"in {data['region']} | Deployed: "
                    f"{d['deployed']}/{d['total_stacks']} | In Progress: "
                    f"{d['in_progress']} | Failed: {d['failed']} | "
                    f"N/A: {d['not_applicable']} | "
                    f"Resources: {d['total_resources']}"
                )
        except Exception as exc:  # noqa: BLE001 — a poll loop must survive any single failure
            logger.error("poll failed: %s", exc)
        time.sleep(15)
