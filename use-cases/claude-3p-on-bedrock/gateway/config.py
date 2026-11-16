"""Schema and gateway.yaml rendering for the claude-3p-on-bedrock use case.

Deliberately free of CDK imports: the schema, the team -> policy mapping and
the rendered file are unit tested on their own
(tests/test_claude_3p_on_bedrock_use_case.py).

Everything lives under `use_cases: claude-3p-on-bedrock:` in platform.yaml.
Customer-specific values (hostname, certificate, tenant, models, Claude Code
version) are required keys with no hidden defaults: a missing one fails at
`deploy.sh design`, before anything touches AWS.

Every gateway.yaml key emitted here is from Anthropic's reference:
https://code.claude.com/docs/en/claude-apps-gateway-config
Keys that need a minimum gateway version are gated on `claude_version`, because
an older gateway refuses to start when it finds them.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
# pydantic's regex engine has no look-ahead, so the 253-char limit is max_length.
_HOST = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
_SEMVER = r"^\d+\.\d+\.\d+$"
# Geo prefixes of Bedrock cross-region inference profiles; stripping one
# yields the foundation model the profile routes to (IAM needs both ARNs).
_PROFILE_PREFIXES = ("global", "us", "eu", "apac", "au", "jp", "us-gov")
_LOG_RETENTION_DAYS = (30, 90, 180, 365)  # same set the platform accepts

# Floor for the gateway server: in-flight stream drain on SIGTERM (2.1.274),
# which the ECS stop timeout relies on, and enduser.sub on Desktop telemetry.
MIN_CLAUDE_VERSION = "2.1.274"
# Minimum gateway versions for optional keys (from the config reference).
_FEATURE_MIN_VERSION = {
    "guardrail": "2.1.281",
    "guardrail.input_tagging": "2.1.316",
    "policy_mode: layered": "2.1.316",
}

CONFIG_DIR = "/config"  # task-scoped volume the init container writes to
CONFIG_PATH = f"{CONFIG_DIR}/gateway.yaml"
DB_NAME = "claude_gateway"
# Encoded config budget inside the 64 KiB task-definition limit.
MAX_ENCODED_CONFIG_BYTES = 32 * 1024


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


class _Strict(BaseModel):
    model_config = {"extra": "forbid"}


class OidcConfig(_Strict):
    """Microsoft Entra ID app registration used by the gateway (confidential web client)."""

    tenant_id: str = Field(pattern=_GUID)
    client_id: str = Field(pattern=_GUID)
    # Secrets Manager NAME holding the app registration's client secret.
    client_secret_name: str = Field(min_length=1)

    @field_validator("client_secret_name")
    @classmethod
    def _secret_name_not_suffix_shaped(cls, v: str) -> str:
        # Found live: ECS references the secret by partial ARN, and Secrets
        # Manager reads a trailing "-" + 6 characters as the random ARN suffix
        # (e.g. ".../entra-client-secret" -> secret ".../entra-client"), so every
        # task fails with AccessDenied. AWS: don't end secret names that way.
        if re.search(r"-[A-Za-z0-9]{6}$", v):
            raise ValueError(
                f"client_secret_name {v!r} ends with '-' plus 6 characters, which Secrets "
                "Manager mistakes for its ARN suffix; ECS then can't read it. Use e.g. "
                "'claude-3p/entra-oidc'"
            )
        return v

    allowed_email_domains: list[str] = Field(min_length=1)
    # Entra App Role values that may sign in at all. Empty: any user in the
    # allowed domains who can get a token for the app (control that in Entra
    # with "Assignment required").
    allowed_roles: list[str] = Field(default_factory=list)
    # Entra emits App Roles under `roles` (readable names); groups would be
    # Object-ID GUIDs under `groups`. Anthropic's Entra note recommends roles.
    groups_claim: str = "roles"
    # B2C/ADFS-style tenants may carry the address in upn/preferred_username.
    email_claim: str | list[str] | None = None
    extra_auth_params: dict[str, str] = Field(default_factory=dict)

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"


class PrivateZone(_Strict):
    name: str = Field(pattern=_HOST, max_length=253)
    # Import an existing private zone already associated with the platform VPC.
    # Omitted: the stack creates the zone and associates it with the VPC.
    id: str | None = Field(default=None, pattern=r"^Z[A-Z0-9]{1,31}$")


class DnsConfig(_Strict):
    """How developer machines resolve `hostname` to the internal ALB.

    customer (default): the customer's corporate DNS holds a CNAME from
      `hostname` to the ALB's DNS name (stack output AlbDnsName). An internal
      ALB's DNS name is publicly resolvable to its PRIVATE node IPs, so no AWS
      DNS service is needed; a CNAME, not an A record, because node IPs change.
    route53: the stack writes an alias record into a Route 53 private hosted
      zone on the platform VPC (created, or imported with private_zone.id), for
      customers who forward the zone to a Route 53 Resolver inbound endpoint.
    """

    mode: Literal["customer", "route53"] = "customer"
    private_zone: PrivateZone | None = None

    @model_validator(mode="after")
    def _zone_matches_mode(self) -> DnsConfig:
        if self.mode == "route53" and self.private_zone is None:
            raise ValueError("dns.mode route53 needs dns.private_zone")
        if self.mode == "customer" and self.private_zone is not None:
            raise ValueError("dns.private_zone is only used with dns.mode: route53")
        return self


class ModelConfig(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    label: str = Field(min_length=1)
    # Inference-profile id (global.anthropic.claude-opus-5) or a full
    # inference-profile / application-inference-profile / provisioned ARN.
    bedrock: str = Field(min_length=1)
    # Foundation model behind an inference-profile ARN (anthropic.claude-...).
    # Bedrock authorizes the profile AND its foundation model, and an ARN does
    # not say which model it routes to, so ARN profiles must name it.
    foundation_model: str | None = Field(
        default=None, pattern=r"^[a-z0-9-]+\.[A-Za-z0-9.:-]+$"
    )

    @model_validator(mode="after")
    def _arn_needs_foundation_model(self) -> ModelConfig:
        is_profile_arn = (
            self.bedrock.startswith("arn:") and "inference-profile/" in self.bedrock
        )
        if is_profile_arn and not self.foundation_model:
            raise ValueError(
                f"models.{self.id}: an inference-profile ARN needs foundation_model"
            )
        return self


class _PolicyBody(_Strict):
    # Model ids from `models:` this audience may use. Enforced server-side at
    # /v1/messages and in the model picker. [] denies every model.
    models: list[str]
    # Model new sessions start on. Without it Claude Code starts on its
    # built-in Opus default, which 400s when the policy leaves it out
    # (Anthropic: "Start sessions on a model the policy allows"). Default:
    # the first entry of `models`.
    start_model: str | None = None
    # Claude Code tools denied (permissions.deny), e.g. WebFetch, WebSearch,
    # "Read(./.env)". Client-side, so defense in depth, not a hard boundary.
    deny_tools: list[str] = Field(default_factory=list)
    # Any other Claude Code managed setting, passed through verbatim into the
    # policy's settings block (the gateway validates it at boot).
    settings: dict[str, Any] = Field(default_factory=dict)
    # Claude Desktop / Cowork managed settings for this audience, passed through
    # verbatim into the policy's `desktop` block, e.g. managedMcpServers (an
    # array). The gateway validates them at boot. On `default:` they reach
    # everyone, because every team inherits the base layer.
    desktop_settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def _no_shadow_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        clash = {"availableModels", "enforceAvailableModels", "model"} & v.keys()
        if clash:
            raise ValueError(
                f"set {sorted(clash)} through `models` / `start_model`, not settings"
            )
        return v

    @model_validator(mode="after")
    def _start_model_listed(self) -> _PolicyBody:
        if self.start_model and self.start_model not in self.models:
            raise ValueError(
                f"start_model {self.start_model!r} is not in this policy's models"
            )
        return self

    @property
    def effective_start_model(self) -> str | None:
        return self.start_model or (self.models[0] if self.models else None)


class Team(_PolicyBody):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    # Entra App Role values (exact, case-sensitive) whose members are this team.
    entra_roles: list[str] = Field(min_length=1)

    @field_validator("entra_roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        if any(not r.strip() for r in v):
            raise ValueError(
                "entra_roles entries must be non-empty (an empty one stops the gateway at boot)"
            )
        # Entra App Role values can't contain spaces; one that does was pasted by
        # mistake (found in the wizard test) and would silently match no one.
        bad = [r for r in v if re.search(r"\s", r)]
        if bad:
            raise ValueError(
                f"entra_roles {bad}: an Entra App Role value has no spaces"
            )
        return v


class DefaultPolicy(_PolicyBody):
    """Everyone signed in, and the base layer every team inherits from."""


class GuardrailConfig(_Strict):
    id: str = Field(min_length=1)  # guardrail id or full ARN
    version: str = Field(pattern=r"^([0-9]+|DRAFT)$")  # string: a bare 1 fails boot
    input_tagging: (
        Literal[
            "last_message", "user_messages", "all_messages", "all_messages_and_system"
        ]
        | None
    ) = None


class DatabaseConfig(_Strict):
    instance_class: str = Field(default="t4g.micro", pattern=r"^[a-z0-9]+\.[a-z0-9]+$")
    allocated_storage_gib: int = Field(default=20, ge=20, le=1000)
    multi_az: bool = False
    backup_retention_days: int = Field(default=7, ge=1, le=35)
    max_connections: int = Field(default=5, ge=1, le=100)


class ServiceConfig(_Strict):
    desired_count: int = Field(default=2, ge=1, le=20)
    cpu: Literal[256, 512, 1024, 2048, 4096] = 512
    memory_mib: int = Field(default=1024, ge=512, le=30720)
    # How long a replaced task keeps serving open streams during a rolling
    # deploy (ALB deregistration delay). ECS sends SIGTERM only after it, and
    # the ALB closes remaining connections when it ends, so this, not the
    # gateway's 25 s SIGTERM drain, bounds a response that survives a deploy.
    drain_seconds: int = Field(default=300, ge=30, le=3600)


class TelemetryConfig(_Strict):
    # sidecar: the gateway relays the CLIENTS' OTLP metrics (Claude Code,
    # Desktop, Cowork, stamped with user identity) to an ADOT collector in the
    # task, which writes them to CloudWatch with SigV4. Two documented costs:
    # it needs CLAUDE_GATEWAY_ALLOW_LOOPBACK=1 (relaxes the SSRF loopback block
    # for every configured URL), and it pushes OTEL_EXPORTER_OTLP_ENDPOINT to
    # clients, which shows each interactive developer a security approval
    # dialog once.
    mode: Literal["off", "sidecar"] = "off"
    collector_image: str = Field(
        default="public.ecr.aws/aws-observability/aws-otel-collector:v0.51.0",
        pattern=r"^[^\s:@]+(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$",
    )

    @field_validator("collector_image")
    @classmethod
    def _pinned(cls, v: str) -> str:
        if v.endswith(":latest"):
            raise ValueError("pin collector_image to a version or digest, not :latest")
        return v


# The gateway fills this in at boot from the task environment, which ECS reads
# from the platform's SSM parameter <ssm_prefix>/gateway/url at deploy time.
AGENTCORE_URL_VAR = "AGENTCORE_GATEWAY_URL"
# Loopback port Claude Code listens on during MCP sign-in. Entra must list
# http://localhost:<port>/callback (Claude Desktop uses http://127.0.0.1:<port>/callback;
# Entra accepts any port there, but registering this one keeps both explicit).
AGENTCORE_CALLBACK_PORT = 53280


class AgentCoreTools(_Strict):
    """The platform's AgentCore Gateway as an MCP server in Claude Code and Cowork.

    Each person signs in to it with Entra ID (the same tenant as `oidc`), so the
    platform must run identity.mode: direct with this app as identity.client_id.
    No OAuth proxy: Claude Desktop and Claude Code take the client ID, tenant
    and scope directly, and Entra accepts Claude's `resource` value because the
    AgentCore Gateway URL is registered as an Application ID URI on this app.
    """

    # Application (client) ID of the Entra app that represents the AgentCore
    # Gateway API: the platform's identity.client_id in direct mode.
    client_id: str = Field(pattern=_GUID)
    # Scope exposed under "Expose an API" on that app.
    scope: str = Field(default="gateway.invoke", pattern=r"^[A-Za-z0-9._-]{1,120}$")
    # Name of the server in Claude Code and Cowork; tool names start with it.
    name: str = Field(default="agentcore", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    # Team names that get the tools. Empty: everyone (the base layer).
    teams: list[str] = Field(default_factory=list)

    @property
    def scopes(self) -> str:
        return f"api://{self.client_id}/{self.scope} offline_access"


class Claude3pConfig(_Strict):
    # Pinned Claude Code release the gateway runs. Current stable:
    #   curl -fsSL https://downloads.claude.ai/claude-code-releases/stable
    claude_version: str = Field(pattern=_SEMVER)
    # Private DNS name developers use. /login refuses gateways that resolve to
    # public addresses, so it lives in a private zone on the platform VPC.
    hostname: str = Field(pattern=_HOST, max_length=253)
    # Imported ACM certificate for `hostname`, in the stack's region.
    certificate_arn: str = Field(
        pattern=r"^arn:aws[a-z-]*:acm:[a-z0-9-]+:\d{12}:certificate/[0-9a-f-]+$"
    )
    # The certificate chains to a private CA: the in-VPC probe skips TLS
    # verification (its HTTP checks still run).
    certificate_private_ca: bool = False
    dns: DnsConfig = Field(default_factory=DnsConfig)
    # Networks developers connect FROM (VPN client pool, corporate ranges).
    # The ALB admits only these on 443. Not the VPC CIDR.
    client_cidrs: list[str] = Field(min_length=1)
    # CIDRs of the subnets the internal ALB is in (the platform VPC's private
    # subnets). Lets the gateway resolve real client IPs from X-Forwarded-For
    # for per-IP rate limits and audit. If wrong, every client looks like the
    # ALB: keep it in sync with the platform VPC layout.
    trusted_proxies: list[str] = Field(default_factory=list)
    # Also enforce client_cidrs INSIDE the gateway (access_control.allow_cidrs),
    # on top of the ALB security group. Needs trusted_proxies. The in-VPC probe
    # is then refused like any non-client address, so verify.py checks the
    # refusal (403) on the OAuth metadata path instead of a 200, and relies on
    # /readyz for boot health.
    gateway_ip_allowlist: bool = False
    oidc: OidcConfig
    session_ttl_hours: int = Field(default=1, ge=1, le=24)
    bedrock_region: str | None = Field(
        default=None, pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$"
    )
    models: list[ModelConfig] = Field(min_length=1)
    # Teams, first match wins (top to bottom) unless policy_mode is layered.
    teams: list[Team] = Field(default_factory=list)
    default: DefaultPolicy
    policy_mode: Literal["first_match", "layered"] = "first_match"
    # Opt every policy in to Claude Desktop / Cowork (`desktop: {}` on the base).
    desktop: bool = True
    # The platform's AgentCore Gateway (web search and other targets) as an MCP
    # server in Claude Code and, with `desktop`, in Cowork.
    agentcore_tools: AgentCoreTools | None = None
    # Entra App Roles with full spend-limit admin rights (gateway Admin API).
    # Caps themselves are set live through the API, not in this file.
    spend_admin_roles: list[str] = Field(default_factory=list)
    guardrail: GuardrailConfig | None = None
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    # true: RDS (snapshot + deletion protection), JWT secret, ECR and logs
    # survive stack deletion. Turn on once spend or audit data matters.
    retain: bool = False
    log_retention_days: int = 90

    @field_validator("client_cidrs", "trusted_proxies")
    @classmethod
    def _cidrs(cls, v: list[str]) -> list[str]:
        for c in v:
            if ipaddress.ip_network(c, strict=True).version != 4:
                raise ValueError(
                    f"{c}: IPv4 only (internal dual-stack ALBs break /login)"
                )
        return v

    @field_validator("log_retention_days")
    @classmethod
    def _retention(cls, v: int) -> int:
        if v not in _LOG_RETENTION_DAYS:
            raise ValueError(f"log_retention_days must be one of {_LOG_RETENTION_DAYS}")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> Claude3pConfig:
        errors: list[str] = []
        if not self.desktop:
            errors += [
                f"{where}.desktop_settings needs desktop: true"
                for where, body in [("default", self.default)]
                + [(f"teams.{t.name}", t) for t in self.teams]
                if body.desktop_settings
            ]
        v = _ver(self.claude_version)
        if v < _ver(MIN_CLAUDE_VERSION):
            errors.append(f"claude_version must be >= {MIN_CLAUDE_VERSION}")
        for feature, used in (
            ("guardrail", self.guardrail is not None),
            (
                "guardrail.input_tagging",
                bool(self.guardrail and self.guardrail.input_tagging),
            ),
            ("policy_mode: layered", self.policy_mode == "layered"),
        ):
            if used and v < _ver(_FEATURE_MIN_VERSION[feature]):
                errors.append(
                    f"{feature} needs claude_version >= {_FEATURE_MIN_VERSION[feature]}"
                )

        if self.dns.private_zone:
            zone = self.dns.private_zone.name
            if not (self.hostname == zone or self.hostname.endswith("." + zone)):
                errors.append(
                    f"hostname {self.hostname!r} is not inside dns.private_zone {zone!r}"
                )

        model_ids = [m.id for m in self.models]
        if len(model_ids) != len(set(model_ids)):
            errors.append("models[].id must be unique")
        catalog = set(model_ids)
        for label, body in [("default", self.default)] + [
            (f"teams.{t.name}", t) for t in self.teams
        ]:
            unknown = sorted(set(body.models) - catalog)
            if unknown:
                errors.append(f"{label}.models {unknown} not in models[]")

        names = [t.name for t in self.teams]
        if len(names) != len(set(names)):
            errors.append("teams[].name must be unique")
        if self.agentcore_tools:
            at = self.agentcore_tools
            unknown_teams = sorted(set(at.teams) - set(names))
            if unknown_teams:
                errors.append(f"agentcore_tools.teams {unknown_teams} not in teams[]")
            for where, body in [("default", self.default)] + [
                (f"teams.{t.name}", t) for t in self.teams
            ]:
                if at.name in (body.settings.get("managedMcpServers") or {}) or any(
                    isinstance(e, dict) and e.get("name") == at.name
                    for e in body.desktop_settings.get("managedMcpServers") or []
                ):
                    errors.append(
                        f"{where}: a managedMcpServers entry is named {at.name!r}, "
                        "like agentcore_tools.name; rename one of them"
                    )
        if self.policy_mode == "first_match":
            # A team whose roles are all claimed by earlier teams can never match.
            seen: set[str] = set()
            for t in self.teams:
                if set(t.entra_roles) <= seen:
                    errors.append(
                        f"teams.{t.name} is unreachable: every entra_role is matched by an "
                        "earlier team (first match wins; reorder, or set policy_mode: layered)"
                    )
                seen |= set(t.entra_roles)

        for c in self.client_cidrs:
            for p in self.trusted_proxies:
                # Anthropic: a developer source inside the trusted-proxy range
                # could spoof X-Forwarded-For.
                if ipaddress.ip_network(c).overlaps(ipaddress.ip_network(p)):
                    errors.append(f"client_cidrs {c} overlaps trusted_proxies {p}")
        if any(not r.strip() for r in self.spend_admin_roles + self.oidc.allowed_roles):
            errors.append(
                "role lists must not contain empty entries (the gateway stops at boot)"
            )
        if self.gateway_ip_allowlist and not self.trusted_proxies:
            # Without trusted proxies every request's client IP is the ALB's,
            # so an allow-list of developer CIDRs would refuse everyone.
            errors.append("gateway_ip_allowlist needs trusted_proxies")
        if not placeholders_allowed():
            errors += placeholder_values(self)
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def public_url(self) -> str:
        return f"https://{self.hostname}"

    def region_for_bedrock(self, stack_region: str) -> str:
        return self.bedrock_region or stack_region


# Example values from docs/platform.example.yaml and the CI fixtures. A copied
# file that still holds one is refused, the same way the platform refuses its
# own placeholders; CI sets PLATFORM_ALLOW_PLACEHOLDERS=1 to synthesize them.
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"
_PLACEHOLDER_RE = re.compile(
    r"example\.(com|org|net|internal)$|replace|change.?me|^<.*>$", re.IGNORECASE
)


def placeholders_allowed() -> bool:
    return os.environ.get("PLATFORM_ALLOW_PLACEHOLDERS") == "1"


def placeholder_values(cfg: Claude3pConfig) -> list[str]:
    found = []
    for field, value in (
        ("oidc.tenant_id", cfg.oidc.tenant_id),
        ("oidc.client_id", cfg.oidc.client_id),
        (
            "agentcore_tools.client_id",
            cfg.agentcore_tools.client_id if cfg.agentcore_tools else "",
        ),
    ):
        if value == _ZERO_GUID:
            found.append(f"{field} is the example value: put your Entra ID here")
    if _PLACEHOLDER_RE.search(cfg.hostname):
        found.append(
            f"hostname {cfg.hostname!r} is the example value: put your gateway hostname here"
        )
    if _ZERO_GUID in cfg.certificate_arn or ":000000000000:" in cfg.certificate_arn:
        found.append(
            "certificate_arn is the example value: put your ACM certificate ARN here"
        )
    found += [
        f"allowed_email_domains {d!r} is the example value: put your email domain here"
        for d in cfg.oidc.allowed_email_domains
        if _PLACEHOLDER_RE.search(d)
    ]
    return found


def load(config: dict | None) -> Claude3pConfig:
    """Validate the raw platform.yaml block. Raises pydantic.ValidationError."""
    return Claude3pConfig.model_validate(config or {})


def _settings_block(body: _PolicyBody, *, base: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"availableModels": list(body.models)}
    if base:
        # Inherited by every team: picker and /v1/messages both restricted.
        block["enforceAvailableModels"] = True
    if body.effective_start_model:
        block["model"] = body.effective_start_model
    extra = dict(body.settings)
    if body.deny_tools:
        perms = dict(extra.pop("permissions", {}) or {})
        perms["deny"] = list(perms.get("deny", [])) + list(body.deny_tools)
        extra["permissions"] = perms
    block.update(extra)
    return block


def policies(cfg: Claude3pConfig) -> list[dict[str, Any]]:
    """teams + default -> managed.policies (teams first, catch-all base last)."""
    # Tool rules go under `cli` (terminal, IDEs, Agent SDK). Anthropic's other
    # key, `code`, would also reach Desktop's Code tab, but with Desktop
    # opted in it turns off WebSearch in Cowork, Chat and the Code tab, and the
    # Code tab then refuses to start on machines whose managed settings don't
    # name this gateway. Model access is enforced server-side either way.
    key = "cli"
    at = cfg.agentcore_tools
    out: list[dict[str, Any]] = []
    for t in cfg.teams:
        pol: dict[str, Any] = {
            "match": {"groups": list(t.entra_roles)},
            key: _settings_block(t, base=False),
        }
        if t.desktop_settings:
            pol["desktop"] = dict(t.desktop_settings)
        if at and t.name in at.teams:
            _add_agentcore(pol, key, cfg)
        out.append(pol)
    base: dict[str, Any] = {"match": {}, key: _settings_block(cfg.default, base=True)}
    if cfg.desktop:
        # cli + desktop: the gateway warns at boot unless each policy says not
        # to offer cli settings to the Code tab (through bootstrapUrl they would
        # not apply anyway). Set explicitly: inheritance is undocumented.
        for p in [*out, base]:
            p["serve_to_desktop"] = False
    if cfg.desktop:
        # On the base layer: every team inherits the opt-in (an empty block
        # is enough) and these Desktop settings.
        base["desktop"] = dict(cfg.default.desktop_settings)
    if at and not at.teams:
        _add_agentcore(base, key, cfg)  # everyone: every team inherits the base layer
    out.append(base)
    return out


def _add_agentcore(policy: dict[str, Any], key: str, cfg: Claude3pConfig) -> None:
    """Add the AgentCore Gateway server to one policy, for Claude Code (`cli`,
    an object keyed by name) and Claude Desktop/Cowork (`desktop`, an array).
    The URL stays a ${VAR} reference that the gateway resolves at boot."""
    at = cfg.agentcore_tools
    assert at is not None
    url = f"${{{AGENTCORE_URL_VAR}}}"
    block = policy[key]
    block["managedMcpServers"] = {
        **(block.get("managedMcpServers") or {}),
        at.name: {
            "type": "http",
            "url": url,
            "oauth": {
                "clientId": at.client_id,
                "callbackPort": AGENTCORE_CALLBACK_PORT,
                "scopes": at.scopes,
            },
        },
    }
    if cfg.desktop:
        desk = policy.setdefault("desktop", {})
        desk["managedMcpServers"] = [
            *(desk.get("managedMcpServers") or []),
            {
                "name": at.name,
                "transport": "http",
                "url": url,
                "oauth": {
                    "clientId": at.client_id,
                    "tenantId": cfg.oidc.tenant_id,
                    "scope": at.scopes,
                },
            },
        ]


def foundation_model_id(bedrock_id: str) -> str | None:
    """global.anthropic.claude-opus-5 -> anthropic.claude-opus-5; ARN -> None."""
    if bedrock_id.startswith("arn:"):
        return None
    head, _, rest = bedrock_id.partition(".")
    return rest if head in _PROFILE_PREFIXES and rest else bedrock_id


def bedrock_resource_arns(
    cfg: Claude3pConfig, region: str, account: str, partition: str = "aws"
) -> list[str]:
    """Exactly the inference profiles and foundation models the catalog names.

    Bedrock authorizes the profile AND the foundation model it routes to;
    global profiles route to any commercial region, hence the region wildcard.
    """
    arns: set[str] = set()
    for m in cfg.models:
        if m.bedrock.startswith("arn:"):
            arns.add(m.bedrock)
            if m.foundation_model:
                arns.add(
                    f"arn:{partition}:bedrock:*::foundation-model/{m.foundation_model}"
                )
            continue
        fm = foundation_model_id(m.bedrock)
        if fm != m.bedrock:
            arns.add(
                f"arn:{partition}:bedrock:{region}:{account}:inference-profile/{m.bedrock}"
            )
        arns.add(f"arn:{partition}:bedrock:*::foundation-model/{fm}")
    return sorted(arns)


def render_gateway_yaml(cfg: Claude3pConfig, stack_region: str) -> str:
    """gateway.yaml as delivered to the task. No secret values: credentials are
    ${VAR} references resolved at boot from the task's Secrets Manager env."""
    oidc: dict[str, Any] = {
        "issuer": cfg.oidc.issuer,
        "client_id": cfg.oidc.client_id,
        "client_secret": "${OIDC_CLIENT_SECRET}",
        "allowed_email_domains": cfg.oidc.allowed_email_domains,
        "groups_claim": cfg.oidc.groups_claim,
    }
    if cfg.oidc.allowed_roles:
        oidc["allowed_groups"] = cfg.oidc.allowed_roles
    if cfg.oidc.email_claim:
        oidc["email_claim"] = cfg.oidc.email_claim
    if cfg.oidc.extra_auth_params:
        oidc["extra_auth_params"] = cfg.oidc.extra_auth_params

    listen: dict[str, Any] = {
        "host": "0.0.0.0",  # nosec B104 - container bind; only the internal ALB reaches it
        "port": 8080,
        "public_url": cfg.public_url,
    }
    if cfg.trusted_proxies:
        listen["trusted_proxies"] = cfg.trusted_proxies

    upstream: dict[str, Any] = {
        "provider": "bedrock",
        "region": cfg.region_for_bedrock(stack_region),
        "auth": {},
    }
    if cfg.guardrail:
        upstream["guardrail"] = {
            "id": cfg.guardrail.id,
            "version": cfg.guardrail.version,
        }
        if cfg.guardrail.input_tagging:
            upstream["guardrail"]["input_tagging"] = cfg.guardrail.input_tagging

    managed: dict[str, Any] = {"policies": policies(cfg)}
    if cfg.policy_mode == "layered":
        managed = {"policy_mode": "layered", **managed}

    doc: dict[str, Any] = {
        "listen": listen,
        "oidc": oidc,
        "session": {
            "jwt_secret": "${GATEWAY_JWT_SECRET}",
            "ttl_hours": cfg.session_ttl_hours,
        },
        "store": {
            # verify-full: the driver reads only sslmode from the URL; the CA
            # comes from NODE_EXTRA_CA_CERTS baked into the image.
            "postgres_url": f"postgres://${{DB_HOST}}:5432/{DB_NAME}?sslmode=verify-full",
            "username": "${DB_USER}",
            "password": "${DB_PASSWORD}",
            "max_connections": cfg.database.max_connections,
        },
        "upstreams": [upstream],
        "auto_include_builtin_models": False,
        "models": [
            {"id": m.id, "label": m.label, "upstream_model": {"bedrock": m.bedrock}}
            for m in cfg.models
        ],
        "managed": managed,
    }
    if cfg.spend_admin_roles:
        doc["admin"] = {"admin_groups": cfg.spend_admin_roles}
    if cfg.gateway_ip_allowlist:
        doc["access_control"] = {"allow_cidrs": cfg.client_cidrs}
    if cfg.telemetry.mode == "sidecar":
        doc["telemetry"] = {
            # 127.0.0.1, not localhost: the collector binds IPv4 loopback only.
            "forward_to": [{"url": "http://127.0.0.1:4318", "metrics": True}]
        }

    header = (
        "# Generated by use-cases/claude-3p-on-bedrock from platform.yaml. Do not edit.\n"
        "# No secret values: ${VAR} references resolve at boot from the ECS task env.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
