"""platform.yaml — the declarative deployment config for the accelerator.

One file describes a deployment: project identity, multi-account strategy,
IdP, agent pattern, gateway tools, security controls, observability. The five
workshop profiles ship as presets/*.yaml built from these same models, so
presets double as validated fixtures.

Validation is error-ACCUMULATING (pydantic collects every failure in one
pass): a participant with three typos gets three messages, not three deploy
cycles. Validate without touching AWS:

    python -m infra_utils.platform_config platform.yaml

Precedence when app.py consumes this (see load note below):
    env var  >  cdk context  >  platform.yaml  >  model defaults
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


class DeploymentConfig(BaseModel):
    """Multi-account strategy. See docs/MULTI_ACCOUNT.md.

    centralized — everything in one account (the default; today's behavior).
    distributed — each team/workload account runs its own full copy of this
        file; org guardrails (terraform/org-guardrails) apply org-wide.
    federated — shared services (gateway, identity, memory, observability)
        live in platform_account; workload_accounts run agent runtimes that
        consume them cross-account.
    """

    strategy: Literal["centralized", "distributed", "federated"] = "centralized"
    platform_account: str = ""
    workload_accounts: list[str] = Field(default_factory=list)

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


def load_platform_config(path: str | Path) -> PlatformConfig:
    """Parse and validate a platform.yaml. Raises pydantic.ValidationError
    with EVERY problem in the file, not just the first."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return PlatformConfig.model_validate(raw)


def _main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: python -m infra_utils.platform_config <platform.yaml>",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_platform_config(sys.argv[1])
    except Exception as exc:  # noqa: BLE001 — the point is printing them all
        print(f"INVALID: {sys.argv[1]}\n{exc}", file=sys.stderr)
        return 1
    print(f"OK: {sys.argv[1]}")
    print(config.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
