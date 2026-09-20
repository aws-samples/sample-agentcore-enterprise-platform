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

import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# Where the Web Search built-in gateway connector exists (launch regions).
WEB_SEARCH_REGIONS = {"us-east-1", "eu-west-1", "ap-northeast-1"}

_ACCOUNT_RE = r"^\d{12}$"
_REGION_RE = r"^[a-z]{2}(-[a-z]+)+-\d$"

# ── Placeholders ──
# A sentinel that validates deploys green and fails at the first sign-in, an
# hour later, with an IdP error that says nothing about the config. Every
# check below names the field and says what belongs there. The parity gate
# (scripts/check-contract.sh) synthesizes the shipped presets WITH their
# placeholders in place, so it exports PLATFORM_ALLOW_PLACEHOLDERS=1: the same
# messages then surface as PlatformConfig.warnings instead of errors. Nothing
# in deploy.sh sets it — a real deploy always fails closed.
_SENTINEL_RE = re.compile(
    r"replace|change.?me|\btodo\b|^<.*>$|example\.com", re.IGNORECASE
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)
# Secrets Manager NAMES: alphanumerics and /_+=.@- only. A pasted secret VALUE
# fails this (whitespace, '~'), or the JWT/base64-blob shapes below.
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,200}$")
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.")
# ponytail: a 32+ char run with no separator is called a value, not a name —
# a real name that long without any of /_.-@ is refused too. Upgrade path:
# an allow-list of known-good names, if one ever exists.
_BLOB_RE = re.compile(r"^[A-Za-z0-9+=]{32,}$")
# 111111111111 is deliberately NOT here: it is the parity gate's fake account
# (check-contract.sh) and a real customer cannot be allocated a
# repeating-digit id, so accepting it costs nothing.
_ACCOUNT_SENTINELS = {"000000000000", "123456789012"}


def placeholders_allowed() -> bool:
    return os.environ.get("PLATFORM_ALLOW_PLACEHOLDERS") == "1"


def _refuse_placeholders(messages: list[str]) -> None:
    if messages and not placeholders_allowed():
        raise ValueError("\n".join(messages))


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
    """Deployment posture and multi-account strategy. See docs/MULTI_ACCOUNT.md.

    mode — workshop preserves the accelerator's disposable defaults;
        production enables retained state and requires the controls enforced
        by PlatformConfig._production_controls_are_complete.
    centralized — everything in one account (the default; today's behavior).
    distributed — each team/workload account runs its own full copy of this
        file; org guardrails (terraform/org-guardrails) apply org-wide.
    federated — shared services (auth, gateway, observability account setting)
        live in platform_account; workload_accounts run agent runtimes plus
        their own credential provider, consuming the platform gateway via
        OAuth. The account you deploy into decides what gets deployed: the
        same file works in both.
    """

    mode: Literal["workshop", "production"] = "workshop"
    strategy: Literal["centralized", "distributed", "federated"] = "centralized"
    platform_account: str = ""
    workload_accounts: list[str] = Field(default_factory=list)
    federation: FederationConfig = Field(default_factory=FederationConfig)

    @field_validator("platform_account")
    @classmethod
    def _platform_account_shape(cls, v: str) -> str:
        if v:
            _account_shape("deployment.platform_account", v)
        return v

    @field_validator("workload_accounts")
    @classmethod
    def _workload_account_shapes(cls, v: list[str]) -> list[str]:
        for i, a in enumerate(v):
            _account_shape(f"deployment.workload_accounts[{i}]", a)
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
        # One account cannot be both sides: federated_role() would call it
        # "platform" and silently deploy no agent runtimes there.
        both = sorted(set(self.workload_accounts) & {self.platform_account})
        if both:
            raise ValueError(
                f"account {both[0]} is both deployment.platform_account and in "
                "deployment.workload_accounts — an account is one side of the "
                "federation, not both"
            )
        _refuse_placeholders(deployment_placeholders(self))
        return self


def _account_placeholder_msg(field: str, value: str) -> str:
    return (
        f"{field} is a placeholder ({value!r}): put the 12-digit AWS account id "
        "here (aws sts get-caller-identity --query Account)"
    )


def _account_shape(field: str, v: str) -> None:
    # A word-shaped placeholder (REPLACE_ME) can never synthesize, so it is a
    # hard error even for the parity gate; the digit-shaped sentinels below
    # pass here and are handled by deployment_placeholders().
    if _SENTINEL_RE.search(v):
        raise ValueError(_account_placeholder_msg(field, v))
    if not re.fullmatch(_ACCOUNT_RE, v):
        raise ValueError(f"{field}: not a 12-digit AWS account id: {v!r}")


def deployment_placeholders(deployment: DeploymentConfig) -> list[str]:
    """Sentinel account ids (see the Placeholders block above)."""
    named = [("deployment.platform_account", deployment.platform_account)]
    named += [
        (f"deployment.workload_accounts[{i}]", a)
        for i, a in enumerate(deployment.workload_accounts)
    ]
    out = [
        _account_placeholder_msg(field, value)
        for field, value in named
        if value in _ACCOUNT_SENTINELS
    ]
    # The federation block is what the platform team hands the workload teams;
    # a preset ships it as placeholders so the shape is visible. Any sentinel
    # left in place means the hand-over has not happened.
    fed = deployment.federation
    for field, value in (
        ("deployment.federation.gateway_url", fed.gateway_url),
        ("deployment.federation.issuer_url", fed.issuer_url),
        ("deployment.federation.m2m_client_id", fed.m2m_client_id),
    ):
        if value and _SENTINEL_RE.search(value):
            out.append(
                f"{field} is a placeholder ({value!r}): copy the value from the "
                "platform account's `deploy.sh export` output"
            )
    return out


class IdentityConfig(BaseModel):
    """Who issues the tokens the platform trusts.

    brokered (default) — Cognito is the issuer; a corporate IdP, when set,
        is federated THROUGH it (users sign in at the IdP, Cognito mints the
        token). One issuer whatever the IdP, platform-owned M2M clients,
        works with no IdP at all.
    direct — the corporate IdP IS the issuer; no Cognito user pool is
        deployed. Gateway and runtimes validate the IdP's tokens by audience
        (Entra tokens carry `aud`/`azp`, not Cognito's `client_id`), and the
        M2M credential is the same app registration's client secret.
        Supported for entra_id; okta/ping stay brokered for now.
    """

    idp: Literal["cognito", "entra_id", "okta", "ping"] = "cognito"
    mode: Literal["brokered", "direct"] = "brokered"
    tenant_id: str = ""
    client_id: str = ""
    issuer_url: str = ""
    # Secrets Manager secret NAME — the secret itself never goes in this file.
    client_secret_name: str = ""

    @property
    def direct_issuer_url(self) -> str:
        """The IdP's OIDC issuer when it is the token issuer itself (mode direct).

        Entra: the v2.0 issuer. The app registration must request v2 access
        tokens (api.requestedAccessTokenVersion = 2), or Entra signs tokens
        with the v1 issuer `https://sts.windows.net/<tenant>/` and every
        authorizer rejects them — checked live by scripts/check_identity.py.
        """
        if self.idp == "entra_id":
            return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        return self.issuer_url.rstrip("/")

    @property
    def direct_m2m_scope(self) -> str:
        """client_credentials scope in direct mode: the app's own `.default`,
        so the token's audience is the app itself (Entra rejects a missing
        scope outright)."""
        return f"{self.client_id}/.default"

    @model_validator(mode="after")
    def _federated_idp_fields(self) -> IdentityConfig:
        if self.mode == "direct" and self.idp != "entra_id":
            raise ValueError(
                f"identity.mode 'direct' is supported for idp 'entra_id' today; "
                f"idp {self.idp!r} deploys brokered (through Cognito). Set mode: "
                "brokered or switch the IdP."
            )
        if self.idp == "entra_id" and not self.tenant_id:
            raise ValueError("idp 'entra_id' requires identity.tenant_id")
        if self.idp in ("okta", "ping") and not self.issuer_url:
            raise ValueError(f"idp {self.idp!r} requires identity.issuer_url")
        if self.idp != "cognito" and not self.client_secret_name:
            raise ValueError(
                f"idp {self.idp!r} requires identity.client_secret_name "
                "(a Secrets Manager secret name — never the secret itself)"
            )
        if self.idp != "cognito" and not self.client_id:
            raise ValueError(
                f"idp {self.idp!r} requires identity.client_id (the IdP app "
                "registration's client id)"
            )
        _refuse_placeholders(identity_placeholders(self))
        return self


def identity_placeholders(identity: IdentityConfig) -> list[str]:
    """Sentinel IdP values (see the Placeholders block above). Empty fields
    are not placeholders — whether they are REQUIRED is the model's job."""
    out: list[str] = []
    t = identity.tenant_id
    if t and (not _UUID_RE.match(t) or set(t) <= set("0-")):
        out.append(
            f"identity.tenant_id is a placeholder ({t!r}): put your Entra tenant "
            "(directory) id here — the UUID under Entra admin center > Overview"
        )
    c = identity.client_id
    if c and _SENTINEL_RE.search(c):
        out.append(
            f"identity.client_id is a placeholder ({c!r}): put your IdP app "
            "registration's client id here"
        )
    u = identity.issuer_url
    if u and (not u.startswith("https://") or _SENTINEL_RE.search(u)):
        out.append(
            f"identity.issuer_url is a placeholder ({u!r}): put your IdP's https "
            "issuer URL here (Okta: https://<org>.okta.com/oauth2/default)"
        )
    s = identity.client_secret_name
    if s and (
        _SENTINEL_RE.search(s)
        or not _SECRET_NAME_RE.match(s)
        or _JWT_RE.match(s)
        or _BLOB_RE.match(s)
    ):
        # The value is deliberately not echoed: it may BE the secret.
        out.append(
            "identity.client_secret_name is a placeholder or looks like a secret "
            "VALUE (not shown): it must be the Secrets Manager NAME of a secret "
            "you created, e.g. agentcore/idp-client-secret"
        )
    return out


class MemoryConfig(BaseModel):
    long_term: bool = False
    event_expiry_days: int = Field(default=30, ge=1, le=365)
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
    # Generation 1 preserves the historical physical names. Increment this
    # only for a controlled replacement when AgentCore can no longer update or
    # even read a runtime (for example, its OIDC discovery endpoint was
    # deleted). Keeping it in the manifest makes later deploys converge on the
    # replacement instead of silently trying to recreate the broken name.
    orchestrator_runtime_generation: int = Field(default=1, ge=1, le=99)
    # Empty = no restriction: the runtime roles keep wildcard Bedrock IAM and
    # any MODEL_ID works — today's behavior, byte-identical templates.
    allowed_models: list[str] = Field(default_factory=list)
    a2a: bool = False
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @field_validator("allowed_models")
    @classmethod
    def _allowed_model_shapes(cls, v: list[str]) -> list[str]:
        # A malformed or placeholder entry would render into the runtime
        # role's IAM policy as an ARN that matches nothing — every model
        # invoke fails at runtime with AccessDenied, long after validation
        # said the config was fine. Catch it here instead.
        for m in v:
            if re.search(
                r"replace|example|changeme", m, re.IGNORECASE
            ) or not re.fullmatch(
                r"([a-z]{2,4}\.)?[a-z0-9-]+\.[a-z0-9][a-zA-Z0-9.:_-]*", m
            ):
                raise ValueError(
                    f"not a Bedrock model id: {m!r}. Expected provider.model or a "
                    "cross-region inference profile like "
                    "'us.anthropic.claude-sonnet-4-6'"
                )
        return v

    @model_validator(mode="after")
    def _allowlist_needs_model_id(self) -> AgentsConfig:
        if self.allowed_models:
            if not self.model_id:
                # When model_id is empty, MODEL_ID is never injected and each
                # agent container falls back to its baked-in DEFAULT_MODEL_ID —
                # the allow-list would be silently bypassed. Setting model_id
                # injects MODEL_ID into every runtime (including the A2A
                # sub-agents), closing the bypass.
                raise ValueError(
                    "agents.allowed_models requires agents.model_id: without it "
                    "each agent falls back to its baked-in DEFAULT_MODEL_ID and "
                    "the allow-list is silently bypassed"
                )
            if self.model_id not in self.allowed_models:
                raise ValueError(
                    f"agents.model_id {self.model_id!r} is not in "
                    f"agents.allowed_models {self.allowed_models}"
                )
        return self


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
    # IAM-denies ungoverned inference on the runtime roles; creates a baseline
    # guardrail per runtime and injects it into the agents.
    require_guardrails: bool = False
    cedar: CedarConfig = Field(default_factory=CedarConfig)
    traceability: bool = False
    org_id: str = ""

    @field_validator("org_id")
    @classmethod
    def _org_id_shape(cls, v: str) -> str:
        # Shape is always enforced. Sentinel values with a valid shape are
        # warnings only for the parity gate and remain hard errors on deploy.
        if v and not re.fullmatch(r"o-[a-z0-9]{10,32}", v):
            raise ValueError(
                f"not an AWS Organizations id: {v!r}. Find yours with: "
                "aws organizations describe-organization --query Organization.Id"
            )
        _refuse_placeholders(security_org_placeholders(v))
        return v

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


def security_org_placeholders(org_id: str) -> list[str]:
    if org_id and re.search(r"replace|example|changeme", org_id, re.IGNORECASE):
        return [
            (
                f"security.org_id is a placeholder ({org_id!r}): find yours with "
                "`aws organizations describe-organization --query Organization.Id`"
            )
        ]
    return []


class ObservabilityConfig(BaseModel):
    transaction_search: bool = True
    # CloudWatch alarms + SNS ops topic + the platform dashboard.
    alarms: bool = False
    alarm_email: str = ""  # empty = topic exists, no email subscription
    log_retention_days: int = 30

    @field_validator("log_retention_days")
    @classmethod
    def _log_retention_supported(cls, v: int) -> int:
        if v not in {30, 90, 180, 365}:
            raise ValueError("must be one of 30, 90, 180, or 365 days")
        return v

    @field_validator("alarm_email")
    @classmethod
    def _alarm_email_shape(cls, v: str) -> str:
        # Empty is fine — the topic still deploys for manual subscription.
        # A placeholder is not: SNS mails a confirmation link to an inbox
        # nobody reads and every alarm after that goes nowhere.
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError(
                f"not a subscribable email address: {v!r}. Use a real inbox "
                "or leave it empty."
            )
        _refuse_placeholders(observability_placeholders(v))
        return v


def observability_placeholders(alarm_email: str) -> list[str]:
    if alarm_email and _SENTINEL_RE.search(alarm_email):
        return [
            (
                "observability.alarm_email is a placeholder: use a real monitored "
                "inbox; SNS confirmation and alarm delivery must reach an operator"
            )
        ]
    return []


# ── Migration ──
# An existing agent (someone else's container) moves onto the platform. The
# block describes where it comes from, how the platform should run it, and
# what it must still reach privately — the contract the adapter (M2), the EC2
# target (M3) and the docs consume. Contract-only here: app.py reads the
# to_env() names once those land.
_ENV_NAME_RE = r"^[A-Z][A-Z0-9_]*$"
_SECRETISH_KEY_RE = re.compile(r"secret|password|token|key$", re.IGNORECASE)
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


class MigrationBuild(BaseModel):
    context: str  # docker build context, relative to the repo root
    dockerfile: str = "Dockerfile"  # relative to context

    model_config = {"extra": "forbid"}


class MigrationSource(BaseModel):
    """The customer's agent as it runs today."""

    platform: Literal[
        "openshift", "kubernetes", "ec2", "ecs", "lambda", "on-prem", "other"
    ]
    image: str = ""  # pre-built image reference — XOR build
    build: MigrationBuild | None = None  # build here (yields arm64 for AgentCore)
    registry_secret_name: str = ""  # Secrets Manager NAME holding docker-login creds
    port: int | None = Field(default=None, ge=1, le=65535)
    invoke_path: str = ""  # where the container takes a request (adapter mode)
    health_path: str = "/"  # what the adapter polls for /ping
    trigger: Literal["webhook", "schedule", "http", "queue"] = "http"
    # Plain configuration. Anything secret-shaped is refused here on purpose:
    # env values render into the task definition / runtime config in clear.
    env: dict[str, str] = Field(default_factory=dict)
    # ENV NAMES only; each value lives in Secrets Manager under
    # <project>/<environment>/migration/<NAME> (see migration_plan()).
    secrets: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("invoke_path", "health_path")
    @classmethod
    def _paths_are_absolute(cls, v: str) -> str:
        if v and not v.startswith("/"):
            raise ValueError(f"must start with '/', got {v!r}")
        return v

    @field_validator("env")
    @classmethod
    def _env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.fullmatch(_ENV_NAME_RE, k):
                raise ValueError(
                    f"env key {k!r} is not an environment variable name "
                    "(UPPER_SNAKE_CASE)"
                )
            if _SECRETISH_KEY_RE.search(k):
                raise ValueError(
                    f"env key {k!r} looks like a secret; list its NAME under "
                    "migration.source.secrets instead and store the value in "
                    "Secrets Manager (env values are rendered in clear)"
                )
        return v

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if not re.fullmatch(_ENV_NAME_RE, s)]
        if bad:
            raise ValueError(
                f"secrets must be environment variable NAMES (UPPER_SNAKE_CASE), "
                f"not values: {bad}"
            )
        return v


class MigrationTarget(BaseModel):
    # agentcore — AgentCore Runtime (arm64, the 8080 /invocations contract);
    # ec2 — ECS on EC2 inside the platform VPC, for amd64-only images.
    runtime: Literal["agentcore", "ec2"] = "agentcore"
    # adapter — the platform wraps the container to speak the AgentCore
    # contract; native — the image already does.
    mode: Literal["adapter", "native"] = "adapter"

    model_config = {"extra": "forbid"}


class MigrationNetwork(BaseModel):
    """What the migrated agent must still reach on the customer side."""

    private_dependencies: list[str] = Field(default_factory=list)  # hostnames
    connectivity: Literal["vpn", "transit-gateway", "none"] = "none"
    dns_forwarders: list[str] = Field(default_factory=list)  # IPv4 resolvers
    ca_bundle_secret_name: str = ""  # Secrets Manager NAME of a private CA bundle

    model_config = {"extra": "forbid"}

    @field_validator("private_dependencies")
    @classmethod
    def _hostnames(cls, v: list[str]) -> list[str]:
        bad = [h for h in v if not _HOSTNAME_RE.match(h)]
        if bad:
            raise ValueError(
                f"private_dependencies must be hostnames (no scheme, port or "
                f"path): {bad}"
            )
        return v

    @field_validator("dns_forwarders")
    @classmethod
    def _ipv4(cls, v: list[str]) -> list[str]:
        for ip in v:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError as exc:
                raise ValueError(
                    f"dns_forwarders must be IPv4 addresses: {ip!r}"
                ) from exc
        return v


class MigrationConfig(BaseModel):
    """Migrate an existing agent onto the platform.

    Stack names do NOT change: the migrated agent deploys as
    `<prefix>-runtime-orchestrator` on both targets, so verify, dashboard and
    destroy keep working with zero contract churn (owner decision, final).
    When this block is present `agents.pattern` no longer selects the runtime
    image — the source does — but it is still emitted as AGENT_PATTERN so
    existing consumers keep working.

    Cross-field rules that touch `security` live on PlatformConfig; the
    non-fatal ones surface as `warnings`.
    """

    source: MigrationSource
    target: MigrationTarget = Field(default_factory=MigrationTarget)
    network: MigrationNetwork = Field(default_factory=MigrationNetwork)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _source_and_mode(self) -> MigrationConfig:
        src, mode = self.source, self.target.mode
        if bool(src.image) == bool(src.build):
            raise ValueError(
                "migration.source needs exactly one of image (pre-built reference) "
                "or build (context + dockerfile)"
            )
        if mode == "adapter" and not (src.port and src.invoke_path):
            raise ValueError(
                "target.mode 'adapter' requires migration.source.port and "
                "migration.source.invoke_path (where the adapter forwards "
                "/invocations to)"
            )
        if mode == "native" and (src.port or src.invoke_path):
            raise ValueError(
                "target.mode 'native' means the image already speaks the AgentCore "
                "contract (8080, /invocations, /ping) — drop migration.source.port "
                "and migration.source.invoke_path, or use mode 'adapter'"
            )
        return self

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.target.runtime == "agentcore" and self.source.image:
            out.append(
                "AgentCore Runtime is arm64-only and a pre-built image "
                f"({self.source.image}) cannot be verified for it before deploy. "
                "Prefer migration.source.build (built arm64 here) or "
                "migration.target.runtime: ec2 for an amd64-only image."
            )
        net = self.network
        if not net.private_dependencies and (
            net.dns_forwarders or net.ca_bundle_secret_name
        ):
            out.append(
                "migration.network.dns_forwarders / ca_bundle_secret_name are set "
                "but private_dependencies is empty — nothing will use them."
            )
        return out


# ── Use cases ──
# A use case is a self-contained product integration under use-cases/<name>/:
# a manifest (this model), a CDK stack, a verify script, and a walkthrough.
# It consumes the platform through its published interface (the SSM parameter
# namespace + Cognito tokens — docs/PLATFORM_INTERFACE.md), never through core
# stack internals, so contributors and the core can evolve independently.
# Nothing deploys unless platform.yaml names the use case under `use_cases:`.
USE_CASES_DIR = Path(__file__).resolve().parents[1] / "use-cases"


class UseCaseManifest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    owner: str  # alias or team — who reviews changes to this folder
    summary: str
    # Core stack suffixes this use case needs in the footprint (e.g. gateway).
    requires: list[str] = Field(default_factory=list)
    # Stack suffixes this use case adds. Namespaced so a contribution can
    # never collide with (or masquerade as) a core stack.
    stacks: list[str] = Field(min_length=1)
    entry: str = "stack.py"  # module exposing build(app, platform_ctx, config)

    model_config = {"extra": "forbid"}

    @field_validator("stacks")
    @classmethod
    def _stacks_are_namespaced(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if not s.startswith("uc-")]
        if bad:
            raise ValueError(f"use-case stack suffixes must start with 'uc-': {bad}")
        return v


def discover_use_cases(root: Path = USE_CASES_DIR) -> dict[str, UseCaseManifest]:
    """Load every use-cases/<name>/manifest.yaml. A broken manifest is a hard
    error naming the file — a contribution must not half-load."""
    found: dict[str, UseCaseManifest] = {}
    if not root.is_dir():
        return found
    for mf in sorted(root.glob("*/manifest.yaml")):
        # use-cases/_template/ is what `deploy.sh usecase new` copies from; its
        # manifest holds {{tokens}} and is not a use case.
        if mf.parent.name.startswith("_"):
            continue
        manifest = UseCaseManifest.model_validate(yaml.safe_load(mf.read_text()))
        if manifest.name != mf.parent.name:
            raise ValueError(
                f"{mf}: manifest name {manifest.name!r} must match its "
                f"directory {mf.parent.name!r}"
            )
        found[manifest.name] = manifest
    return found


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
    # Opt-in use cases: name → that use case's own config block (passed to its
    # build() untouched; {} to enable with defaults). Names must exist under
    # use-cases/ — validated below so a typo cannot silently deploy nothing.
    use_cases: dict[str, dict] = Field(default_factory=dict)
    # Absent = no migration; everything behaves exactly as before.
    migration: MigrationConfig | None = None

    model_config = {"extra": "forbid"}  # a typo'd key is an error, not a no-op

    @field_validator("use_cases")
    @classmethod
    def _use_cases_exist(cls, v: dict[str, dict]) -> dict[str, dict]:
        if not v:
            return v
        available = discover_use_cases()
        unknown = sorted(set(v) - set(available))
        if unknown:
            raise ValueError(
                f"unknown use case(s) {unknown}; available: {sorted(available)} "
                "(a use case is a directory under use-cases/ with a manifest.yaml)"
            )
        return v

    @model_validator(mode="after")
    def _guardrails_need_an_attachable_model(self) -> PlatformConfig:
        # The Claude Agent SDK's Bedrock path has no way to attach a Bedrock
        # Guardrail, so the require_guardrails IAM deny would block every
        # inference from those patterns — a bricked agent, not a hardened one.
        if self.security.require_guardrails and self.agents.pattern.startswith(
            "claude-sdk"
        ):
            raise ValueError(
                f"security.require_guardrails is incompatible with agents.pattern "
                f"{self.agents.pattern!r}: the Claude Agent SDK cannot attach a "
                "Bedrock Guardrail to its inference calls, so the IAM deny would "
                "block every inference from this pattern"
            )
        return self

    @model_validator(mode="after")
    def _migration_needs_the_vpc(self) -> PlatformConfig:
        # Validation rather than a new expected_stacks() branch: networking is
        # already in the footprint whenever security.networking is true, so
        # requiring the flag keeps the contract to one rule per stack.
        m = self.migration
        if m is None:
            return self
        if m.target.runtime == "ec2" and not self.security.networking:
            raise ValueError(
                "migration.target.runtime 'ec2' runs in the platform VPC: set "
                "security.networking: true"
            )
        if m.network.private_dependencies:
            if not self.security.networking:
                raise ValueError(
                    "migration.network.private_dependencies needs the agent in the "
                    "VPC: set security.networking: true"
                )
            if m.network.connectivity == "none":
                raise ValueError(
                    "migration.network.private_dependencies needs a path to the "
                    "customer network: set migration.network.connectivity to "
                    "'vpn' or 'transit-gateway'"
                )
        return self

    @model_validator(mode="after")
    def _production_controls_are_complete(self) -> PlatformConfig:
        if self.deployment.mode != "production":
            return self

        missing: list[str] = []

        def require(condition: bool, field: str, expectation: str) -> None:
            if not condition:
                missing.append(f"{field}: {expectation}")

        require(
            self.identity.idp != "cognito",
            "identity.idp",
            "use an enterprise IdP (entra_id, okta, or ping)",
        )
        require(self.security.networking, "security.networking", "set true")
        require(
            self.security.cloudtrail_alerting,
            "security.cloudtrail_alerting",
            "set true",
        )
        require(
            self.security.resource_policies,
            "security.resource_policies",
            "set true",
        )
        require(self.security.egress_filter, "security.egress_filter", "set true")
        require(
            self.security.require_guardrails,
            "security.require_guardrails",
            "set true",
        )
        require(self.security.cedar.enabled, "security.cedar.enabled", "set true")
        require(
            self.security.cedar.mode == "ENFORCE",
            "security.cedar.mode",
            "set ENFORCE",
        )
        require(self.security.traceability, "security.traceability", "set true")
        require(bool(self.security.org_id), "security.org_id", "provide the AWS Org id")
        require(bool(self.agents.model_id), "agents.model_id", "select a model")
        require(
            bool(self.agents.allowed_models),
            "agents.allowed_models",
            "provide a non-empty model allow-list",
        )
        require(
            self.gateway.web_search != "auto",
            "gateway.web_search",
            "choose on or off explicitly",
        )
        require(
            self.observability.transaction_search,
            "observability.transaction_search",
            "set true",
        )
        require(self.observability.alarms, "observability.alarms", "set true")
        require(
            bool(self.observability.alarm_email),
            "observability.alarm_email",
            "provide a monitored inbox",
        )
        if missing:
            raise ValueError(
                "deployment.mode 'production' requires all production controls:\n- "
                + "\n- ".join(missing)
            )
        return self

    @property
    def warnings(self) -> list[str]:
        """Everything non-fatal about this file: placeholders that validation
        let through because PLATFORM_ALLOW_PLACEHOLDERS=1 (otherwise they are
        errors and the model never exists), plus the migration warnings."""
        out = (
            identity_placeholders(self.identity)
            + deployment_placeholders(self.deployment)
            + security_org_placeholders(self.security.org_id)
            + observability_placeholders(self.observability.alarm_email)
        )
        # The migration block (PR #63) carries its own warnings; getattr keeps
        # this property valid on a tree where that field does not exist yet.
        migration = getattr(self, "migration", None)
        return out + (migration.warnings if migration else [])

    @property
    def web_search_enabled(self) -> bool:
        """Resolve gateway.web_search 'auto' against the launch regions."""
        if self.gateway.web_search == "auto":
            return self.region in WEB_SEARCH_REGIONS
        return self.gateway.web_search == "on"

    def expected_stacks(self, account: str = "") -> list[str]:
        """The exact stack names this config makes app.py synthesize.

        This is the deployment contract: deploy plans, verification, the
        dashboard, and destroy all consume this list instead of growing their
        own opinion of the footprint. It deliberately duplicates app.py's
        stack-existence logic — scripts/check-contract.sh synthesizes every
        preset and fails CI when the two drift, which is what keeps the
        duplication honest. If you add a stack to app.py, add it here and the
        parity check goes green again.

        `account` matters only for strategy 'federated', where the account
        deployed into decides the role (platform: shared services, no agent
        runtimes; workload: runtimes + own memory/identity, no auth/gateway).
        """
        role = self.federated_role(account)
        prefix = f"{self.project}-{self.environment}"
        stacks: list[str] = []
        if self.security.networking:
            stacks.append(f"{prefix}-networking")
        if self.security.cloudtrail_alerting:
            stacks.append(f"{prefix}-security")
        if role != "workload":
            stacks.append(f"{prefix}-auth")
        stacks.append(f"{prefix}-identity")
        if role != "platform":
            stacks.append(f"{prefix}-memory")
        if role != "workload":
            stacks.append(f"{prefix}-gateway")
        if role != "platform":
            stacks.append(f"{prefix}-runtime-orchestrator")
            if self.agents.a2a:
                stacks.append(f"{prefix}-runtime-code-agent")
                stacks.append(f"{prefix}-runtime-research-agent")
        stacks.append(f"{prefix}-observability")

        # Enabled use cases ride at the end. Their `requires` are core stack
        # suffixes that must be in THIS footprint — checked here because the
        # footprint is role-dependent (a federated workload account has no
        # local gateway, so a gateway-requiring use case belongs platform-side).
        if self.use_cases:
            manifests = discover_use_cases()
            suffixes = {s.removeprefix(f"{prefix}-") for s in stacks}
            for name in sorted(self.use_cases):
                missing = sorted(set(manifests[name].requires) - suffixes)
                if missing:
                    raise ValueError(
                        f"use case {name!r} requires {missing}, absent from this "
                        f"footprint (strategy={self.deployment.strategy}"
                        f"{', role=' + role if role else ''}). Enable the "
                        "prerequisites, or deploy the use case on the side of "
                        "the federation that runs them."
                    )
                stacks.extend(f"{prefix}-{s}" for s in manifests[name].stacks)
        return stacks

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


def apply_environment_overrides(
    config: PlatformConfig, environ: dict[str, str] | None = None
) -> PlatformConfig:
    """Return the effective config after deploy.sh's supported env overrides.

    The Design phase used to print the manifest unchanged even when the same
    command would pass explicit environment values to CDK. That made the
    read-back actively misleading for identity changes. Keep this conversion
    close to ``to_env`` so the human plan and deployment share one vocabulary.
    Empty values are ignored, matching deploy.sh's fill-if-unset behavior.
    """
    env = environ if environ is not None else dict(os.environ)
    raw = config.model_dump()

    def put(path: tuple[str, ...], value: object) -> None:
        target = raw
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    scalar_paths = {
        "PROJECT_NAME": ("project",),
        "ENVIRONMENT": ("environment",),
        "AWS_REGION": ("region",),
        "DEPLOYMENT_MODE": ("deployment", "mode"),
        "DEPLOYMENT_STRATEGY": ("deployment", "strategy"),
        "PLATFORM_ACCOUNT": ("deployment", "platform_account"),
        "IDP_TYPE": ("identity", "idp"),
        "IDP_MODE": ("identity", "mode"),
        "IDP_TENANT_ID": ("identity", "tenant_id"),
        "IDP_CLIENT_ID": ("identity", "client_id"),
        "IDP_ISSUER_URL": ("identity", "issuer_url"),
        "IDP_CLIENT_SECRET_NAME": ("identity", "client_secret_name"),
        "AGENT_PATTERN": ("agents", "pattern"),
        "MODEL_ID": ("agents", "model_id"),
        "CEDAR_MODE": ("security", "cedar", "mode"),
        "ORG_ID": ("security", "org_id"),
        "ALARM_EMAIL": ("observability", "alarm_email"),
    }
    for key, path in scalar_paths.items():
        if env.get(key):
            put(path, env[key])

    bool_paths = {
        "ENABLE_A2A": ("agents", "a2a"),
        "USE_LONG_TERM_MEMORY": ("agents", "memory", "long_term"),
        "ENABLE_NETWORKING": ("security", "networking"),
        "ENABLE_SECURITY": ("security", "cloudtrail_alerting"),
        "ENABLE_RESOURCE_POLICIES": ("security", "resource_policies"),
        "ENABLE_EGRESS_FILTER": ("security", "egress_filter"),
        "REQUIRE_GUARDRAILS": ("security", "require_guardrails"),
        "ENABLE_CEDAR": ("security", "cedar", "enabled"),
        "ENABLE_TRACEABILITY": ("security", "traceability"),
        "ENABLE_TRANSACTION_SEARCH": ("observability", "transaction_search"),
        "ENABLE_ALARMS": ("observability", "alarms"),
    }
    for key, path in bool_paths.items():
        if not env.get(key):
            continue
        normalized = env[key].lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false, got {env[key]!r}")
        put(path, normalized == "true")

    if env.get("ENABLE_WEB_SEARCH"):
        normalized = env["ENABLE_WEB_SEARCH"].lower()
        if normalized not in {"true", "false"}:
            raise ValueError(
                "ENABLE_WEB_SEARCH must be true or false, "
                f"got {env['ENABLE_WEB_SEARCH']!r}"
            )
        put(("gateway", "web_search"), "on" if normalized == "true" else "off")
    if env.get("ALLOWED_MODELS"):
        put(
            ("agents", "allowed_models"),
            [item.strip() for item in env["ALLOWED_MODELS"].split(",") if item.strip()],
        )
    for key, path in {
        "ORCHESTRATOR_RUNTIME_GENERATION": (
            "agents",
            "orchestrator_runtime_generation",
        ),
        "LTM_TOP_K": ("agents", "memory", "top_k"),
        "LTM_RELEVANCE_SCORE": ("agents", "memory", "relevance_score"),
        "MEMORY_EVENT_EXPIRY_DAYS": ("agents", "memory", "event_expiry_days"),
        "LOG_RETENTION_DAYS": ("observability", "log_retention_days"),
    }.items():
        if env.get(key):
            put(path, env[key])

    return PlatformConfig.model_validate(raw)


def allowed_model_resources(models: list[str]) -> list[str]:
    """IAM resource ARNs for the runtime role's BedrockModels statement.

    A geo-prefixed entry (us., eu., ...) is a cross-region inference-profile
    id and emits BOTH the profile ARN and the base foundation-model ARN:
    invoking through a profile also requires foundation-model permissions in
    the profile's target regions — profile-only scoping breaks invokes.
    Foundation-model ARNs get a trailing '*' because Bedrock model ids carry
    version/context-window suffixes ('-v1:0', ':0:200k') that configs
    commonly omit.
    """
    resources: list[str] = []
    for m in models:
        prefix_match = re.match(r"^[a-z]{2,4}\.", m)
        base = m[prefix_match.end() :] if prefix_match else m
        if prefix_match and "." in base:
            resources.append(f"arn:aws:bedrock:*:*:inference-profile/{m}")
        else:
            base = m
        resources.append(f"arn:aws:bedrock:*::foundation-model/{base}*")
    return list(dict.fromkeys(resources))  # dedupe, order preserved


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
        "DEPLOYMENT_MODE": config.deployment.mode,
        "DEPLOYMENT_STRATEGY": config.deployment.strategy,
        "PLATFORM_ACCOUNT": config.deployment.platform_account,
        "IDP_TYPE": config.identity.idp,
        "IDP_MODE": config.identity.mode,
        "IDP_TENANT_ID": config.identity.tenant_id,
        "IDP_CLIENT_ID": config.identity.client_id,
        "IDP_ISSUER_URL": config.identity.issuer_url,
        "IDP_CLIENT_SECRET_NAME": config.identity.client_secret_name,
        "AGENT_PATTERN": config.agents.pattern,
        "ORCHESTRATOR_RUNTIME_GENERATION": str(
            config.agents.orchestrator_runtime_generation
        ),
        "MODEL_ID": config.agents.model_id,
        "ALLOWED_MODELS": ",".join(config.agents.allowed_models),
        "ENABLE_A2A": str(config.agents.a2a).lower(),
        "USE_LONG_TERM_MEMORY": str(config.agents.memory.long_term).lower(),
        "MEMORY_EVENT_EXPIRY_DAYS": str(config.agents.memory.event_expiry_days),
        "LTM_TOP_K": str(config.agents.memory.top_k),
        "LTM_RELEVANCE_SCORE": str(config.agents.memory.relevance_score),
        "ENABLE_WEB_SEARCH": str(config.web_search_enabled).lower(),
        "ENABLE_NETWORKING": str(config.security.networking).lower(),
        "ENABLE_SECURITY": str(config.security.cloudtrail_alerting).lower(),
        "ENABLE_RESOURCE_POLICIES": str(config.security.resource_policies).lower(),
        "ENABLE_EGRESS_FILTER": str(config.security.egress_filter).lower(),
        "REQUIRE_GUARDRAILS": str(config.security.require_guardrails).lower(),
        "ENABLE_CEDAR": str(config.security.cedar.enabled).lower(),
        "CEDAR_MODE": config.security.cedar.mode,
        "ENABLE_TRACEABILITY": str(config.security.traceability).lower(),
        "ORG_ID": config.security.org_id,
        "ENABLE_TRANSACTION_SEARCH": str(
            config.observability.transaction_search
        ).lower(),
        "ENABLE_ALARMS": str(config.observability.alarms).lower(),
        "ALARM_EMAIL": config.observability.alarm_email,
        "LOG_RETENTION_DAYS": str(config.observability.log_retention_days),
        "MIGRATION_ENABLED": str(config.migration is not None).lower(),
    }
    if config.migration:
        m = config.migration
        build = m.source.build
        pairs.update(
            {
                "MIGRATION_SOURCE_PLATFORM": m.source.platform,
                "MIGRATION_SOURCE_IMAGE": m.source.image,
                "MIGRATION_BUILD_CONTEXT": build.context if build else "",
                "MIGRATION_BUILD_DOCKERFILE": build.dockerfile if build else "",
                "MIGRATION_REGISTRY_SECRET_NAME": m.source.registry_secret_name,
                "MIGRATION_PORT": str(m.source.port or ""),
                "MIGRATION_INVOKE_PATH": m.source.invoke_path,
                "MIGRATION_HEALTH_PATH": m.source.health_path,
                "MIGRATION_TRIGGER": m.source.trigger,
                # ponytail: comma-joined KEY=VALUE — a value containing ',' would
                # split wrong on the shell side; upgrade path is a JSON blob.
                "MIGRATION_ENV": ",".join(f"{k}={v}" for k, v in m.source.env.items()),
                "MIGRATION_SECRETS": ",".join(m.source.secrets),
                "MIGRATION_TARGET_RUNTIME": m.target.runtime,
                "MIGRATION_TARGET_MODE": m.target.mode,
                "MIGRATION_PRIVATE_DEPENDENCIES": ",".join(
                    m.network.private_dependencies
                ),
                "MIGRATION_CONNECTIVITY": m.network.connectivity,
                "MIGRATION_DNS_FORWARDERS": ",".join(m.network.dns_forwarders),
                "MIGRATION_CA_BUNDLE_SECRET_NAME": m.network.ca_bundle_secret_name,
            }
        )
    return {k: v for k, v in pairs.items() if v != ""}


def migration_secret_name(config: PlatformConfig, env_name: str) -> str:
    """Secrets Manager name for one migration.source.secrets entry."""
    return f"{config.project}/{config.environment}/migration/{env_name}"


def migration_plan(config: PlatformConfig, account: str = "") -> list[str]:
    """Render the migration as plain text: what moves where, what the operator
    must create first, and how to check each private dependency. Pure — no
    I/O, no AWS — so deploy.sh and tests print the same thing."""
    m = config.migration
    if m is None:
        return ["No migration: block in this manifest — nothing to plan."]
    src, tgt, net = m.source, m.target, m.network
    origin = (
        f"image {src.image}"
        if src.image
        else f"build {src.build.context} ({src.build.dockerfile})"
    )
    lines = [
        f"Migration plan: {config.project}-{config.environment} ({config.region})",
        f"  Source: {src.platform}, {origin}, trigger {src.trigger}",
        (
            f"  Target: {tgt.runtime} runtime, {tgt.mode} mode "
            f"(deploys as {config.project}-{config.environment}-runtime-orchestrator)"
        ),
        (
            f"  agents.pattern {config.agents.pattern!r} is ignored: the runtime "
            "image comes from migration.source"
        ),
        "",
        "Stacks:",
    ]
    lines += [f"  {s}" for s in config.expected_stacks(account)]
    if tgt.mode == "adapter":
        base = f"http://127.0.0.1:{src.port}"
        lines += [
            "",
            "Adapter mapping (AgentCore contract -> your container):",
            f"  POST /invocations -> {base}{src.invoke_path}",
            f"  GET  /ping        -> {base}{src.health_path}",
        ]
    if src.env:
        lines += ["", "Environment (in clear):"]
        lines += [f"  {k}={v}" for k, v in src.env.items()]
    if src.secrets or src.registry_secret_name:
        lines += ["", "Secrets Manager (create these BEFORE deploying):"]
        lines += [
            f"  {name} <- {migration_secret_name(config, name)}" for name in src.secrets
        ]
        if src.registry_secret_name:
            lines.append(f"  registry login <- {src.registry_secret_name}")
    if net.private_dependencies:
        lines += [
            "",
            (
                f"Private dependencies (connectivity: {net.connectivity}); "
                "reachability check for each:"
            ),
        ]
        lines += [
            (
                f"  {host}: from a host in the VPC: "
                f"curl -sS -o /dev/null -w '%{{http_code}}' https://{host}/"
            )
            for host in net.private_dependencies
        ]
        if net.dns_forwarders:
            lines.append(f"  DNS forwarders: {', '.join(net.dns_forwarders)}")
        if net.ca_bundle_secret_name:
            lines.append(f"  CA bundle secret: {net.ca_bundle_secret_name}")
    if config.security.networking:
        lines += [
            "",
            (
                "Networking: VPC mode is on. Outbound traffic leaves through the "
                "NAT gateway EIP (networking stack output) — allow-list it on "
                "the customer side."
            ),
        ]
    if m.warnings:
        lines += ["", "Warnings:"]
        lines += [f"  - {w}" for w in m.warnings]
    return lines


def sign_in_lines(config: PlatformConfig, account: str = "") -> list[str]:
    """The Design read-back for identity: who issues tokens, and what the
    customer must register on the IdP side BEFORE build — the redirect URI in
    brokered mode was derived by hand (and once for the wrong account) until
    this printed it."""
    ident = config.identity
    if ident.idp == "cognito":
        return ["  Sign-in: cognito"]
    if ident.mode == "direct":
        return [
            f"  Sign-in: {ident.idp} (direct — the IdP issues tokens; no Cognito)",
            f"    issuer: {ident.direct_issuer_url}",
            f"    audience: {ident.client_id}",
            (
                "    IdP app must: have a service principal in the tenant and "
                "request v2 access tokens (deploy.sh verify checks both)"
            ),
        ]
    acct = account or "<account-id>"
    redirect = (
        f"https://{config.project}-{config.environment}-{acct}.auth."
        f"{config.region}.amazoncognito.com/oauth2/idpresponse"
    )
    return [
        f"  Sign-in: {ident.idp} via Cognito (brokered)",
        f"    register this redirect URI on the IdP app: {redirect}",
    ]


def design_plan(config: PlatformConfig, account: str = "") -> list[str]:
    """The Design-phase read-back: everything this manifest will make the
    platform do, as plain text, before anything exists. Pure — no I/O, no AWS
    — so `deploy.sh design`, tests and docs print the same thing.

    Composes the pieces that already know their part (expected_stacks, the
    migration plan, warnings) rather than growing a second opinion of any.
    """
    dep = config.deployment
    try:
        role = config.federated_role(account)
    except Exception:  # noqa: BLE001 — an unlisted account is reported in the plan, not raised
        role = None
    stacks = (
        config.expected_stacks(account) if (dep.strategy != "federated" or role) else []
    )
    lines = [
        f"Design: {config.project}-{config.environment} in {config.region}",
        f"  Mode: {dep.mode}",
        f"  Topology: {dep.strategy}"
        + (f", this account is the {role} side" if role else "")
        + (f" (account {account})" if account else ""),
        *sign_in_lines(config, account),
        f"  Agent pattern: {config.agents.pattern}"
        + (f", model {config.agents.model_id}" if config.agents.model_id else "")
        + (" (allow-listed)" if config.agents.allowed_models else "")
        + (
            f", orchestrator runtime generation "
            f"{config.agents.orchestrator_runtime_generation} "
            "(controlled replacement)"
            if config.agents.orchestrator_runtime_generation > 1
            else ""
        ),
        "",
        f"Stacks ({len(stacks)}):"
        if stacks
        else "Stacks: (federated — deploy from a listed account to see a side)",
    ]
    lines += [f"  {s}" for s in stacks]

    on = [
        name
        for name, flag in (
            ("private networking (VPC mode)", config.security.networking),
            ("CloudTrail + alerting", config.security.cloudtrail_alerting),
            ("resource policies", config.security.resource_policies),
            ("egress filter", config.security.egress_filter),
            ("guardrail-enforced inference", config.security.require_guardrails),
            ("Cedar authorization", config.security.cedar.enabled),
            ("traceability", config.security.traceability),
            ("alarms + dashboard", config.observability.alarms),
        )
        if flag
    ]
    lines += [
        "",
        "Controls on: " + (", ".join(on) if on else "none (every control is opt-in)"),
        (
            "Lifecycle: retained stateful resources; "
            f"{config.observability.log_retention_days}-day logs; "
            f"{config.agents.memory.event_expiry_days}-day memory events"
            if dep.mode == "production"
            else "Lifecycle: disposable workshop resources"
        ),
    ]

    if config.use_cases:
        lines += ["", "Use cases:"]
        lines += [f"  {name}  (uc-{name})" for name in sorted(config.use_cases)]
    else:
        lines += ["", "Use cases: none yet (deploy.sh usecase new <name>)"]

    if config.migration:
        # migration_plan() prints the migration block's own warnings; the
        # identity/deployment ones are added below so none is lost.
        mig = migration_plan(config, account)
        cut = mig.index("Warnings:") if "Warnings:" in mig else len(mig)
        lines += [""] + mig[: cut - 1 if cut else 0]
    if config.warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in config.warnings]
    lines += [
        "",
        "Nothing has been deployed. Next: deploy.sh build, then deploy.sh verify.",
    ]
    return lines


def resolve_region(root: Path | None = None) -> str:
    """Resolve the deployment region the way deploy.sh does, so every tool
    (verify, invoke, monitor, check_*) reports on the region the deploy
    actually used instead of silently defaulting to us-east-1:

        env AWS_REGION/AWS_DEFAULT_REGION > platform.yaml > workshop.env
        > AWS profile > us-east-1

    `root` overrides the repository root (tests); PLATFORM_CONFIG overrides
    the manifest path, mirroring deploy.sh.
    """
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region
    root = root or Path(__file__).resolve().parents[1]
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or root / "platform.yaml")
    if manifest.exists():
        try:
            return load_platform_config(manifest).region
        except Exception:  # noqa: BLE001, S110 — deploy fails loudly on a bad
            pass  # manifest; a read-only tool just falls through the chain.
    saved = root / "workshop.env"
    if saved.exists():
        match = re.search(r"^AWS_REGION=([a-z0-9-]+)$", saved.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    import boto3  # lazy: only the fallback needs it

    return boto3.Session().region_name or "us-east-1"


def _main() -> int:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if flags & {"--plan", "--design"} and not args:
        # --plan alone reads the deployment manifest, like deploy.sh does.
        root = Path(__file__).resolve().parents[1]
        args = [os.environ.get("PLATFORM_CONFIG") or str(root / "platform.yaml")]
    action_flags = flags & {"--export", "--stacks", "--plan", "--design"}
    if (
        len(args) != 1
        or len(action_flags) > 1
        or not flags
        <= {"--export", "--stacks", "--plan", "--design", "--effective-env"}
        or ("--effective-env" in flags and not action_flags)
    ):
        print(
            "usage: python -m infra_utils.platform_config "
            "[--export | --stacks | --plan | --design] "
            "[--effective-env] <platform.yaml>",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_platform_config(args[0])
    except ValidationError as exc:
        # Messages only — pydantic's str() echoes the offending input, and for
        # identity.client_secret_name the input may BE the secret.
        print(f"INVALID: {args[0]}", file=sys.stderr)
        for err in exc.errors():
            where = ".".join(str(x) for x in err["loc"]) or "platform.yaml"
            print(
                f"  {where}: {err['msg'].removeprefix('Value error, ')}",
                file=sys.stderr,
            )
        return 1
    except Exception as exc:  # noqa: BLE001 — YAML syntax, missing file: print it all
        print(f"INVALID: {args[0]}\n{exc}", file=sys.stderr)
        return 1
    if "--effective-env" in flags:
        try:
            config = apply_environment_overrides(config)
        except (ValidationError, ValueError) as exc:
            print(f"INVALID effective environment\n{exc}", file=sys.stderr)
            return 1
    if "--export" not in flags:
        # --export is machine-read by deploy.sh with 2>&1; keep it key=value only.
        for warning in config.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    if "--export" in flags:
        for key, value in to_env(config).items():
            print(f"{key}={value}")
        return 0
    if "--stacks" in flags:
        # The contract's view of the footprint; CDK_DEFAULT_ACCOUNT selects
        # the side of a federated deployment, same as app.py.
        for name in config.expected_stacks(os.environ.get("CDK_DEFAULT_ACCOUNT", "")):
            print(name)
        return 0
    if "--design" in flags:
        print("\n".join(design_plan(config, os.environ.get("CDK_DEFAULT_ACCOUNT", ""))))
        return 0
    if "--plan" in flags:
        print(
            "\n".join(migration_plan(config, os.environ.get("CDK_DEFAULT_ACCOUNT", "")))
        )
        return 0
    print(f"OK: {args[0]}")
    print(config.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
