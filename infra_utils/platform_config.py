"""platform.yaml — the declarative deployment config for the accelerator.

One file describes a deployment: project identity, multi-account strategy,
IdP, agent pattern, gateway tools, security controls, observability. The five
workshop profiles ship as presets/*.yaml built from these same models, so
presets double as validated fixtures.

Validation is error-ACCUMULATING (pydantic collects every failure in one
pass): a participant with three typos gets three messages, not three deploy
cycles. Validate without touching AWS:

    python -m infra_utils.platform_config platform.yaml

Precedence when consumed (matches the pre-existing context-over-env order):
    app.py:    cdk context  >  env var  >  platform.yaml  >  legacy defaults
    deploy.sh: explicit env >  platform.yaml  >  workshop.env  >  prompts
platform.yaml is optional — every existing flag keeps working without it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Where the Web Search built-in gateway connector exists (launch regions).
WEB_SEARCH_REGIONS = {"us-east-1", "eu-west-1", "ap-northeast-1"}

_ACCOUNT_RE = r"^\d{12}$"
_REGION_RE = r"^[a-z]{2}(-[a-z]+)+-\d$"


class FederationConfig(BaseModel):
    """Platform-account endpoints a federated WORKLOAD account consumes.

    The platform team fills this block after deploying the platform account
    and hands it to workload teams (all four values are outputs of the auth
    and gateway stacks; none is secret — the M2M client secret itself goes
    into the workload account's OWN Secrets Manager under
    m2m_client_secret_name, never into this file).

    Trust is pure OAuth, verified live (docs/MULTI_ACCOUNT.md): the workload
    account's token vault exchanges these client credentials against the
    platform Cognito token endpoint, and the platform gateway accepts the
    resulting JWT. No cross-account IAM anywhere on the data plane.
    """

    gateway_url: str = ""
    issuer_url: str = ""  # platform Cognito issuer; discovery URL is derived
    m2m_client_id: str = ""
    m2m_client_secret_name: str = ""  # Secrets Manager NAME in the workload account

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer_url.rstrip('/')}/.well-known/openid-configuration"

    @property
    def is_complete(self) -> bool:
        return all(
            (
                self.gateway_url,
                self.issuer_url,
                self.m2m_client_id,
                self.m2m_client_secret_name,
            )
        )


class DeploymentConfig(BaseModel):
    """Multi-account strategy. See docs/MULTI_ACCOUNT.md.

    centralized — everything in one account (the default; today's behavior).
    distributed — each team/workload account runs its own full copy of this
        file; org guardrails (terraform/org-guardrails) apply org-wide.
    federated — shared services (auth, gateway, observability account setting)
        live in platform_account; workload_accounts run agent runtimes plus
        their own credential provider, consuming the platform gateway via
        OAuth. The account you deploy into decides what gets deployed: the
        same file works in both.
    """

    strategy: Literal["centralized", "distributed", "federated"] = "centralized"
    platform_account: str = ""
    workload_accounts: list[str] = Field(default_factory=list)
    federation: FederationConfig = Field(default_factory=FederationConfig)

    @field_validator("platform_account")
    @classmethod
    def _platform_account_shape(cls, v: str) -> str:
        if v and not re.fullmatch(_ACCOUNT_RE, v):
            raise ValueError(f"not a 12-digit AWS account id: {v!r}")
        return v

    @field_validator("workload_accounts")
    @classmethod
    def _workload_account_shapes(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if not re.fullmatch(_ACCOUNT_RE, a)]
        if bad:
            raise ValueError(f"not 12-digit AWS account ids: {bad}")
        return v

    @model_validator(mode="after")
    def _federated_needs_accounts(self) -> DeploymentConfig:
        if self.strategy == "federated":
            if not self.platform_account:
                raise ValueError(
                    "strategy 'federated' requires deployment.platform_account"
                )
            if not self.workload_accounts:
                raise ValueError(
                    "strategy 'federated' requires deployment.workload_accounts"
                )
        return self


class IdentityConfig(BaseModel):
    idp: Literal["cognito", "entra_id", "okta", "ping"] = "cognito"
    tenant_id: str = ""
    client_id: str = ""
    issuer_url: str = ""
    # Secrets Manager secret NAME — the secret itself never goes in this file.
    client_secret_name: str = ""

    @model_validator(mode="after")
    def _federated_idp_fields(self) -> IdentityConfig:
        if self.idp == "entra_id" and not self.tenant_id:
            raise ValueError("idp 'entra_id' requires identity.tenant_id")
        if self.idp in ("okta", "ping") and not self.issuer_url:
            raise ValueError(f"idp {self.idp!r} requires identity.issuer_url")
        if self.idp != "cognito" and not self.client_secret_name:
            raise ValueError(
                f"idp {self.idp!r} requires identity.client_secret_name "
                "(a Secrets Manager secret name — never the secret itself)"
            )
        return self


class MemoryConfig(BaseModel):
    long_term: bool = False
    top_k: int = Field(default=10, ge=1, le=100)
    relevance_score: float = Field(default=0.3, ge=0.0, le=1.0)


AGENT_PATTERNS = (
    "orchestrator",
    "strands-agent",
    "langgraph-agent",
    "claude-sdk-agent",
    "claude-sdk-multi-agent",
    "agui-strands-agent",
    "agui-langgraph-agent",
)


class AgentsConfig(BaseModel):
    pattern: Literal[AGENT_PATTERNS] = "orchestrator"  # type: ignore[valid-type]
    model_id: str = ""  # empty = the pattern's own default
    a2a: bool = False
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class GatewayConfig(BaseModel):
    # auto = on where the built-in connector exists (WEB_SEARCH_REGIONS).
    web_search: Literal["auto", "on", "off"] = "auto"
    tools: list[str] = Field(default_factory=lambda: ["sample-tool"])


class CedarConfig(BaseModel):
    enabled: bool = False
    mode: Literal["LOG_ONLY", "ENFORCE"] = "LOG_ONLY"


class SecurityConfig(BaseModel):
    networking: bool = False  # VPC mode for the runtimes
    cloudtrail_alerting: bool = False  # the security stack (trail + alerting)
    resource_policies: bool = False
    egress_filter: bool = False
    cedar: CedarConfig = Field(default_factory=CedarConfig)
    traceability: bool = False
    org_id: str = ""

    @model_validator(mode="after")
    def _traceability_needs_trail(self) -> SecurityConfig:
        # The EventBridge alerting only fires if CloudTrail management events
        # are recorded — silently useless without the trail (TESTING.md caveat 5).
        if self.traceability and not self.cloudtrail_alerting:
            raise ValueError(
                "security.traceability needs security.cloudtrail_alerting: the "
                "alert rule only fires on CloudTrail management events"
            )
        return self


class ObservabilityConfig(BaseModel):
    transaction_search: bool = True


class PlatformConfig(BaseModel):
    """The root model — `platform.yaml` parses into exactly this."""

    project: str = Field(
        default="agentcore-workshop", pattern=r"^[a-z][a-z0-9-]{2,32}$"
    )
    environment: str = Field(default="dev", pattern=r"^[a-z][a-z0-9]{1,15}$")
    region: str = Field(default="us-east-1", pattern=_REGION_RE)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    model_config = {"extra": "forbid"}  # a typo'd key is an error, not a no-op

    @property
    def web_search_enabled(self) -> bool:
        """Resolve gateway.web_search 'auto' against the launch regions."""
        if self.gateway.web_search == "auto":
            return self.region in WEB_SEARCH_REGIONS
        return self.gateway.web_search == "on"

    def federated_role(self, account: str) -> str | None:
        """Which side of a federated deployment this account is.

        The same platform.yaml works in both accounts — the account you
        deploy into decides what gets deployed. Deploying a federated file
        from an account named in neither list is a hard error: it is the
        config-file version of deploying to the wrong account.
        """
        if self.deployment.strategy != "federated":
            return None
        if account == self.deployment.platform_account:
            return "platform"
        if account in self.deployment.workload_accounts:
            return "workload"
        raise ValueError(
            f"strategy is 'federated' but account {account or '(unset)'} is neither "
            f"deployment.platform_account ({self.deployment.platform_account}) nor in "
            f"deployment.workload_accounts ({self.deployment.workload_accounts})"
        )


def load_platform_config(path: str | Path) -> PlatformConfig:
    """Parse and validate a platform.yaml. Raises pydantic.ValidationError
    with EVERY problem in the file, not just the first."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return PlatformConfig.model_validate(raw)


def to_env(config: PlatformConfig) -> dict[str, str]:
    """Map the schema onto the env-var names deploy.sh and app.py already use.

    Only these names cross the shell boundary; everything else stays in the
    typed model. Empty values are omitted so fill-if-unset logic in deploy.sh
    never clobbers a variable with "".
    """
    pairs = {
        "PROJECT_NAME": config.project,
        "ENVIRONMENT": config.environment,
        "AWS_REGION": config.region,
        "DEPLOYMENT_STRATEGY": config.deployment.strategy,
        "PLATFORM_ACCOUNT": config.deployment.platform_account,
        "IDP_TYPE": config.identity.idp,
        "IDP_TENANT_ID": config.identity.tenant_id,
        "IDP_CLIENT_ID": config.identity.client_id,
        "IDP_ISSUER_URL": config.identity.issuer_url,
        "IDP_CLIENT_SECRET_NAME": config.identity.client_secret_name,
        "AGENT_PATTERN": config.agents.pattern,
        "MODEL_ID": config.agents.model_id,
        "ENABLE_A2A": str(config.agents.a2a).lower(),
        "USE_LONG_TERM_MEMORY": str(config.agents.memory.long_term).lower(),
        "LTM_TOP_K": str(config.agents.memory.top_k),
        "LTM_RELEVANCE_SCORE": str(config.agents.memory.relevance_score),
        "ENABLE_WEB_SEARCH": str(config.web_search_enabled).lower(),
        "ENABLE_NETWORKING": str(config.security.networking).lower(),
        "ENABLE_SECURITY": str(config.security.cloudtrail_alerting).lower(),
        "ENABLE_RESOURCE_POLICIES": str(config.security.resource_policies).lower(),
        "ENABLE_EGRESS_FILTER": str(config.security.egress_filter).lower(),
        "ENABLE_CEDAR": str(config.security.cedar.enabled).lower(),
        "CEDAR_MODE": config.security.cedar.mode,
        "ENABLE_TRACEABILITY": str(config.security.traceability).lower(),
        "ORG_ID": config.security.org_id,
        "ENABLE_TRANSACTION_SEARCH": str(
            config.observability.transaction_search
        ).lower(),
    }
    return {k: v for k, v in pairs.items() if v != ""}


def _main() -> int:
    args = [a for a in sys.argv[1:] if a != "--export"]
    export = "--export" in sys.argv[1:]
    if len(args) != 1:
        print(
            "usage: python -m infra_utils.platform_config [--export] <platform.yaml>",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_platform_config(args[0])
    except Exception as exc:  # noqa: BLE001 — the point is printing them all
        print(f"INVALID: {args[0]}\n{exc}", file=sys.stderr)
        return 1
    if export:
        for key, value in to_env(config).items():
            print(f"{key}={value}")
        return 0
    print(f"OK: {args[0]}")
    print(config.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
