#!/usr/bin/env python3
"""AgentCore Workshop CDK App — modular, progressive deployment.

Stack dependency graph:
    NetworkingStack (optional)
    SecurityStack (optional)
    AuthStack
    IdentityStack ← AuthStack
    MemoryStack ← AuthStack, SecurityStack(optional)
    GatewayStack ← AuthStack
    RuntimeStack (orchestrator) ← AuthStack, GatewayStack, MemoryStack
    RuntimeStack (code-agent, A2A) ← AuthStack
    RuntimeStack (research-agent, A2A) ← AuthStack
    ObservabilityStack ← all resource stacks

Deploy or update an environment through ``scripts/deploy.sh``. The orchestrator
owns consumer-first interface migrations, credential-rotation checkpoints,
and the deployment lock. Use CDK directly only for synth/diff or disposable
test stacks.
"""

import json
import os

import aws_cdk as cdk

from infra_utils.platform_config import (
    PlatformConfig,
    apply_environment_overrides,
    load_platform_config,
    to_env,
)
from stacks.auth_stack import AuthStack
from stacks.gateway_stack import GatewayStack
from stacks.identity_stack import IdentityStack
from stacks.memory_stack import MemoryStack
from stacks.networking_stack import NetworkingStack
from stacks.observability_stack import ObservabilityStack
from stacks.runtime_stack import RuntimeStack
from stacks.security_stack import SecurityStack

app = cdk.App()

# ── platform.yaml (optional declarative config) ──
# Precedence: cdk context > env var > platform.yaml > legacy defaults.
# Loading FAILS the synth on an invalid file (with every error listed) —
# deploying defaults the user didn't write would be worse than stopping.
# Without the file, behavior is identical to before it existed: _yaml_env is
# empty and every lookup falls through to its legacy default (which is why
# resolution goes through to_env() rather than PlatformConfig attributes —
# schema defaults and legacy defaults deliberately differ, e.g. ENABLE_A2A).
_config_path = app.node.try_get_context("platform_config") or os.environ.get(
    "PLATFORM_CONFIG", "platform.yaml"
)
platform_config: PlatformConfig | None = None
_yaml_env: dict[str, str] = {}
if os.path.exists(_config_path):
    platform_config = apply_environment_overrides(
        load_platform_config(_config_path), dict(os.environ)
    )
    _yaml_env = to_env(platform_config)


def cfg(context_key: str, env_key: str, default: str) -> str:
    """context > env > platform.yaml > legacy default."""
    return (
        app.node.try_get_context(context_key)
        or os.environ.get(env_key, "")
        or _yaml_env.get(env_key, "")
        or default
    )


# ── Configuration (context, environment, or platform.yaml) ──
project = cfg("project", "PROJECT_NAME", "agentcore-workshop")
env_name = cfg("environment", "ENVIRONMENT", "dev")
region = (
    app.node.try_get_context("region")
    or os.environ.get("CDK_DEFAULT_REGION", "")
    or _yaml_env.get("AWS_REGION", "")
    or "us-east-1"
)
account = os.environ.get("CDK_DEFAULT_ACCOUNT", "")

cdk_env = cdk.Environment(account=account, region=region)
prefix = f"{project}-{env_name}"
deployment_mode = cfg("deployment_mode", "DEPLOYMENT_MODE", "workshop")
if deployment_mode not in {"workshop", "production"}:
    raise ValueError(
        f"deployment_mode must be 'workshop' or 'production', got {deployment_mode!r}"
    )
production_mode = deployment_mode == "production"
if production_mode and (
    platform_config is None or platform_config.deployment.mode != "production"
):
    raise ValueError(
        "Production mode must be declared in a validated platform.yaml; direct "
        "context/env flags cannot bypass the production control gate."
    )

# Feature flags
enable_networking = cfg("enable_networking", "ENABLE_NETWORKING", "false") == "true"
enable_security = cfg("enable_security", "ENABLE_SECURITY", "false") == "true"
enable_a2a = cfg("enable_a2a", "ENABLE_A2A", "true") == "true"

# Web Search built-in gateway connector: on by default where the connector
# exists, off elsewhere (creating the target in an unsupported region fails the
# deploy). Override either way with enable_web_search=true|false; in
# platform.yaml this is gateway.web_search (auto|on|off, auto = region gate).
WEB_SEARCH_REGIONS = {"us-east-1", "eu-west-1", "ap-northeast-1"}
enable_web_search = (
    cfg(
        "enable_web_search",
        "ENABLE_WEB_SEARCH",
        "true" if region in WEB_SEARCH_REGIONS else "false",
    )
    == "true"
)
idp_type = cfg("idp_type", "IDP_TYPE", "cognito")
# brokered: Cognito issues tokens (the IdP federates through it). direct: the
# IdP issues them and no user pool exists — see IdentityConfig.
idp_mode = cfg("idp_mode", "IDP_MODE", "brokered")
# Internal one-deploy compatibility switch. deploy.sh uses it to keep the old
# secret-bearing CDK export alive while an existing identity stack moves to
# the Secrets Manager reference. It is never set for a normal synthesis.
retain_legacy_m2m_export = (
    app.node.try_get_context("retain_legacy_m2m_export") == "true"
)
retain_legacy_m2m_client = (
    app.node.try_get_context("retain_legacy_m2m_client") == "true"
)
transition_m2m_consumers = (
    app.node.try_get_context("transition_m2m_consumers") == "true"
)
retired_m2m_client_id = app.node.try_get_context("retired_m2m_client_id") or ""


# Security control feature flags (control-library / scope-split model).
# Guardrailed-only Bedrock: every runtime stack gets a baseline guardrail plus an
# IAM deny on inference calls that carry none. The claude-sdk incompatibility is
# validated on the platform.yaml path only (raw -c users are on their own — same
# stance as the rest of app.py).
require_guardrails = cfg("require_guardrails", "REQUIRE_GUARDRAILS", "false") == "true"
enable_resource_policies = (
    cfg("enable_resource_policies", "ENABLE_RESOURCE_POLICIES", "false") == "true"
)
# Egress Lambda interceptor + Bedrock Guardrail on the Gateway (PII masking, prompt injection).
enable_egress_filter = (
    cfg("enable_egress_filter", "ENABLE_EGRESS_FILTER", "false") == "true"
)
# AgentCore Cedar policy engine on the Gateway (explicit read permits; Cedar's implicit
# default-deny covers everything else). cedar_mode is
# LOG_ONLY (evaluate + log) or ENFORCE (block); ships LOG_ONLY for safe rollout.
enable_cedar = cfg("enable_cedar", "ENABLE_CEDAR", "false") == "true"
cedar_mode = cfg("cedar_mode", "CEDAR_MODE", "LOG_ONLY")
# Detective controls: SNS + EventBridge alerting on sensitive AgentCore API calls (item 7).
enable_traceability = (
    cfg("enable_traceability", "ENABLE_TRACEABILITY", "false") == "true"
)
# CloudWatch Transaction Search. Defaults ON because tracing does not work without
# it (X-Ray rejects every span batch with HTTP 400), but it is an account- and
# region-level setting: turn it off where a platform team owns tracing centrally.
enable_transaction_search = (
    cfg("enable_transaction_search", "ENABLE_TRANSACTION_SEARCH", "true") == "true"
)
# CloudWatch alarms + SNS ops topic + the platform dashboard, all in the
# observability stack. alarm_email subscribes an inbox to the topic (SNS sends
# a confirmation link); empty means the topic deploys without a subscription.
enable_alarms = cfg("enable_alarms", "ENABLE_ALARMS", "false") == "true"
alarm_email = cfg("alarm_email", "ALARM_EMAIL", "")
log_retention_days = int(cfg("log_retention_days", "LOG_RETENTION_DAYS", "30"))
# AWS Organizations ID (o-xxxx). Required when enable_resource_policies is on, so the
# in-account-only resource policies can render their aws:PrincipalOrgID deny guard.
org_id = cfg("org_id", "ORG_ID", "")

# Agent pattern selection (from FAST reference patterns)
# Options: strands-agent, langgraph-agent, claude-sdk-agent, claude-sdk-multi-agent,
#          agui-strands-agent, agui-langgraph-agent
agent_pattern = cfg("agent_pattern", "AGENT_PATTERN", "orchestrator")
orchestrator_runtime_generation = int(
    cfg(
        "orchestrator_runtime_generation",
        "ORCHESTRATOR_RUNTIME_GENERATION",
        "1",
    )
)

# ── Migration mode (platform.yaml `migration:` block / MIGRATION_* env) ──
# An existing customer container replaces the agent pattern on the
# runtime-orchestrator stack: the migration adapter (migration-adapter/) is
# built ON TOP of the customer's image and serves the AgentCore contract in
# front of it. A2A stacks are untouched. Env names match platform_config's
# to_env(); the stack args mirror them 1:1.
migration_enabled = cfg("migration_enabled", "MIGRATION_ENABLED", "false") == "true"
migration_kwargs: dict = {}
migration_private_dependencies: list[str] = []
migration_ca_bundle_secret_name = ""
if migration_enabled:
    _migration_target_runtime = cfg(
        "migration_target_runtime", "MIGRATION_TARGET_RUNTIME", "agentcore"
    )
    if _migration_target_runtime != "agentcore":
        raise ValueError(
            "Only migration_target_runtime='agentcore' is implemented. "
            "The ECS-on-EC2 target is not available yet."
        )
    _migration_target_mode = cfg(
        "migration_target_mode", "MIGRATION_TARGET_MODE", "adapter"
    )
    if _migration_target_mode != "adapter":
        raise ValueError(
            "Only migration_target_mode='adapter' is implemented. "
            "Native container deployment is not available yet."
        )
    _migration_env_json = cfg("migration_env_json", "MIGRATION_ENV_JSON", "")
    if _migration_env_json:
        try:
            _migration_env = json.loads(_migration_env_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "MIGRATION_ENV_JSON must be a JSON object of string values"
            ) from exc
        if not isinstance(_migration_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in _migration_env.items()
        ):
            raise ValueError(
                "MIGRATION_ENV_JSON must be a JSON object of string values"
            )
    else:
        # Legacy direct-environment compatibility. New platform.yaml manifests
        # emit MIGRATION_ENV_JSON so commas and equals signs round-trip safely.
        _migration_env_pairs = cfg("migration_env", "MIGRATION_ENV", "")
        _migration_env = dict(
            pair.split("=", 1)
            for pair in _migration_env_pairs.split(",")
            if "=" in pair
        )
    migration_kwargs = {
        "source_image": cfg("migration_source_image", "MIGRATION_SOURCE_IMAGE", ""),
        "build_context": cfg("migration_build_context", "MIGRATION_BUILD_CONTEXT", ""),
        "build_dockerfile": cfg(
            "migration_build_dockerfile", "MIGRATION_BUILD_DOCKERFILE", "Dockerfile"
        ),
        "registry_secret_name": cfg(
            "migration_registry_secret_name", "MIGRATION_REGISTRY_SECRET_NAME", ""
        ),
        "adapter_dir": "migration-adapter",
        "migration_env": _migration_env,
        "migration_secret_names": [
            s.strip()
            for s in cfg("migration_secrets", "MIGRATION_SECRETS", "").split(",")
            if s.strip()
        ],
        "migration_port": cfg("migration_port", "MIGRATION_PORT", "8000"),
        "migration_invoke_path": cfg(
            "migration_invoke_path", "MIGRATION_INVOKE_PATH", "/invocations"
        ),
        "migration_health_path": cfg(
            "migration_health_path", "MIGRATION_HEALTH_PATH", ""
        ),
    }
    migration_private_dependencies = [
        host.strip()
        for host in cfg(
            "migration_private_dependencies",
            "MIGRATION_PRIVATE_DEPENDENCIES",
            "",
        ).split(",")
        if host.strip()
    ]
    migration_ca_bundle_secret_name = cfg(
        "migration_ca_bundle_secret_name",
        "MIGRATION_CA_BUNDLE_SECRET_NAME",
        "",
    )
    if not (migration_kwargs["source_image"] or migration_kwargs["build_context"]):
        raise ValueError(
            "migration is enabled but neither migration_source_image nor "
            "migration_build_context is set — the platform needs to know where "
            "the customer's container comes from (migration.source in platform.yaml)."
        )

# Optional Bedrock model ID override (cross-region inference profile, e.g.
# us.anthropic.claude-sonnet-5). When unset, MODEL_ID is NOT injected into the
# runtimes and each agent pattern falls back to its in-code DEFAULT_MODEL_ID —
# agent code stays the single source of truth for the default.
model_id = cfg("model_id", "MODEL_ID", "")
model_env = {"MODEL_ID": model_id} if model_id else {}

# IAM allow-list for the runtime roles: scopes the Bedrock statement to these
# models. Validation of the model_id/allowed_models combination lives in
# infra_utils/platform_config.py.
allowed_models = [
    m.strip()
    for m in cfg("allowed_models", "ALLOWED_MODELS", "").split(",")
    if m.strip()
]
if production_mode:
    resolved_production_gaps = [
        field
        for field, ready in (
            ("identity.idp", idp_type != "cognito"),
            ("security.networking", enable_networking),
            ("security.cloudtrail_alerting", enable_security),
            ("security.resource_policies", enable_resource_policies),
            ("security.egress_filter", enable_egress_filter),
            ("security.require_guardrails", require_guardrails),
            ("security.cedar.enabled", enable_cedar),
            ("security.cedar.mode", cedar_mode == "ENFORCE"),
            ("security.traceability", enable_traceability),
            ("security.org_id", bool(org_id)),
            ("agents.model_id", bool(model_id)),
            ("agents.allowed_models", bool(allowed_models)),
            (
                "agents.model_id in agents.allowed_models",
                bool(model_id) and model_id in allowed_models,
            ),
            (
                "identity.mode",
                idp_mode != "direct" or idp_type == "entra_id",
            ),
            ("observability.transaction_search", enable_transaction_search),
            ("observability.alarms", enable_alarms),
            ("observability.alarm_email", bool(alarm_email)),
        )
        if not ready
    ]
    if resolved_production_gaps:
        raise ValueError(
            "Resolved CDK context would disable production controls: "
            + ", ".join(resolved_production_gaps)
        )

# Long-term memory configuration
use_long_term_memory = (
    cfg("use_long_term_memory", "USE_LONG_TERM_MEMORY", "false") == "true"
)
ltm_top_k = int(cfg("ltm_top_k", "LTM_TOP_K", "10"))
ltm_relevance_score = float(cfg("ltm_relevance_score", "LTM_RELEVANCE_SCORE", "0.3"))
memory_event_expiry_days = int(
    cfg("memory_event_expiry_days", "MEMORY_EVENT_EXPIRY_DAYS", "30")
)

# IdP config from context or env.
# The IdP client secret is NEVER accepted as plaintext context — only the name of a
# Secrets Manager secret. AuthStack resolves it at deploy time via a
# {{resolve:secretsmanager:...}} dynamic reference so the value never appears in
# process listings or the synthesized template.
idp_config = {
    "tenant_id": cfg("idp_tenant_id", "IDP_TENANT_ID", ""),
    "client_id": cfg("idp_client_id", "IDP_CLIENT_ID", ""),
    "client_secret_name": cfg("idp_client_secret_name", "IDP_CLIENT_SECRET_NAME", ""),
    "issuer_url": cfg("idp_issuer_url", "IDP_ISSUER_URL", ""),
}

# OAuth provider credentials (3LO). Secrets travel as Secrets Manager secret
# NAMES only — a plaintext *_client_secret context key or env var is rejected,
# because context values land in `ps` output, cdk.context.json, and (previously)
# verbatim in the synthesized template. deploy.sh upserts the secret and passes
# the name; see the identity stack for the dynamic-reference rendering.
for _vendor in ("google", "github", "notion"):
    if app.node.try_get_context(f"{_vendor}_client_secret") or os.environ.get(
        f"{_vendor.upper()}_CLIENT_SECRET", ""
    ):
        raise ValueError(
            f"Plaintext '{_vendor}_client_secret' / {_vendor.upper()}_CLIENT_SECRET is "
            f"no longer supported — store it in Secrets Manager and pass "
            f"'{_vendor}_client_secret_name' instead (scripts/deploy.sh does this "
            "automatically when the secret is in the environment)."
        )

google_client_id = cfg("google_client_id", "GOOGLE_CLIENT_ID", "")
google_client_secret_name = cfg(
    "google_client_secret_name", "GOOGLE_CLIENT_SECRET_NAME", ""
)
github_client_id = cfg("github_client_id", "GITHUB_CLIENT_ID", "")
github_client_secret_name = cfg(
    "github_client_secret_name", "GITHUB_CLIENT_SECRET_NAME", ""
)
notion_client_id = cfg("notion_client_id", "NOTION_CLIENT_ID", "")
notion_client_secret_name = cfg(
    "notion_client_secret_name", "NOTION_CLIENT_SECRET_NAME", ""
)

# ── Global Tags ──
cdk.Tags.of(app).add("Project", project)
cdk.Tags.of(app).add("Environment", env_name)
cdk.Tags.of(app).add("ManagedBy", "CDK")
cdk.Tags.of(app).add("DeploymentMode", deployment_mode)

# ═══════════════════════════════════════════════════════════════
# FOUNDATION LAYER
# ═══════════════════════════════════════════════════════════════

# ── Optional: Networking (VPC, subnets, endpoints) ──
networking_stack = None
if enable_networking:
    networking_stack = NetworkingStack(
        app,
        f"{prefix}-networking",
        project_name=project,
        environment=env_name,
        enable_vpc_endpoints=True,
        org_id=org_id,
        migration_private_dependencies=migration_private_dependencies,
        migration_ca_bundle_secret_name=migration_ca_bundle_secret_name,
        log_retention_days=log_retention_days,
        retain_data=production_mode,
        env=cdk_env,
    )

# ── Optional: Security (KMS CMK, CloudTrail) ──
security_stack = None
if enable_security:
    security_stack = SecurityStack(
        app,
        f"{prefix}-security",
        project_name=project,
        environment=env_name,
        enable_kms=True,
        enable_cloudtrail=True,
        retain_data=production_mode,
        env=cdk_env,
    )

# ═══════════════════════════════════════════════════════════════
# MULTI-ACCOUNT ROLE (federated strategy)
# ═══════════════════════════════════════════════════════════════
# The same platform.yaml deploys both sides: the account decides the role.
#   platform account → shared services (auth, identity, gateway, security,
#                      networking, observability), no agent runtimes.
#   workload account → agent runtimes + own memory + own credential provider
#                      that exchanges the platform Cognito M2M credentials.
# Trust is pure OAuth — verified live cross-account, docs/MULTI_ACCOUNT.md.
federated_role = platform_config.federated_role(account) if platform_config else None
is_fed_platform = federated_role == "platform"
is_fed_workload = federated_role == "workload"
fed = platform_config.deployment.federation if platform_config else None
if is_fed_workload and not (fed and fed.is_complete):
    raise ValueError(
        "This account is a federated WORKLOAD account, but deployment.federation "
        "is incomplete. The platform team provides gateway_url, issuer_url, "
        "m2m_client_id, and m2m_client_secret_name (run `deploy.sh export` in the "
        "platform account); the secret itself goes into THIS account's Secrets "
        "Manager under that name."
    )

# ═══════════════════════════════════════════════════════════════
# IDENTITY LAYER
# ═══════════════════════════════════════════════════════════════

# ── Auth (Cognito + federated IdP) — platform side only ──
auth_stack = None
if not is_fed_workload:
    auth_stack = AuthStack(
        app,
        f"{prefix}-auth",
        project_name=project,
        environment=env_name,
        idp_type=idp_type,
        idp_mode=idp_mode,
        idp_config=idp_config,
        retain_legacy_m2m_client=(
            retain_legacy_m2m_export
            or retain_legacy_m2m_client
            or transition_m2m_consumers
        ),
        publish_legacy_m2m_interface=(
            retain_legacy_m2m_export or retain_legacy_m2m_client
        ),
        retain_data=production_mode,
        env=cdk_env,
    )

# Where runtimes/gateway find the issuer and M2M client: the local auth stack,
# or the platform account's (via the federation block) in a workload account.
# Cognito tokens are pinned by `client_id`; an IdP issuing directly (Entra)
# emits `aud`/`azp` and no client_id, so direct mode pins the audience instead
# (infra_utils/jwt_authorizer.py). The M2M token then needs an explicit scope
# — the app's own `.default` — which agents read from GATEWAY_TOKEN_SCOPES.
allowed_audience: list[str] = []
gateway_token_scopes = ""
if is_fed_workload:
    issuer_url = fed.issuer_url
    discovery_url = fed.discovery_url
    m2m_client_id = fed.m2m_client_id
    m2m_client_secret_name = fed.m2m_client_secret_name
    legacy_m2m_client_secret = None
    allowed_clients = [fed.m2m_client_id]
    if idp_mode == "direct":
        allowed_clients, allowed_audience = [], [fed.m2m_client_id]
        gateway_token_scopes = f"{fed.m2m_client_id}/.default"
    elif retired_m2m_client_id:
        allowed_clients.append(retired_m2m_client_id)
else:
    issuer_url = auth_stack.issuer_url
    discovery_url = auth_stack.discovery_url
    use_legacy_m2m_client = (
        retain_legacy_m2m_export or retain_legacy_m2m_client
    ) and not auth_stack.is_direct
    m2m_client_id = (
        auth_stack.legacy_m2m_client_id
        if use_legacy_m2m_client
        else auth_stack.m2m_client_id
    )
    m2m_client_secret_name = (
        auth_stack.legacy_m2m_client_secret_name
        if use_legacy_m2m_client
        else auth_stack.m2m_client_secret_name
    )
    legacy_m2m_client_secret = (
        auth_stack.legacy_m2m_client_secret
        if retain_legacy_m2m_export and not auth_stack.is_direct
        else None
    )
    if auth_stack.is_direct:
        allowed_clients, allowed_audience = [], [auth_stack.app_client_id]
        gateway_token_scopes = f"{auth_stack.app_client_id}/.default"
    else:
        allowed_clients = [auth_stack.app_client_id, auth_stack.m2m_client_id]
        if retired_m2m_client_id:
            allowed_clients.append(retired_m2m_client_id)
        elif transition_m2m_consumers:
            allowed_clients.append(auth_stack.legacy_m2m_client_id)
# Omitted when empty: an empty environment value is an L1 deploy-time reject.
gateway_scope_env = (
    {"GATEWAY_TOKEN_SCOPES": gateway_token_scopes} if gateway_token_scopes else {}
)

# ── Identity (gateway M2M provider + 3LO OAuth providers) ──
# Deployed on BOTH sides of a federation: token vaults are account-local, so a
# workload account needs its own provider (holding the platform M2M
# credentials) even though the issuer lives in the platform account.
identity_stack = IdentityStack(
    app,
    f"{prefix}-identity",
    project_name=project,
    environment=env_name,
    gateway_m2m_client_id=m2m_client_id,
    gateway_m2m_client_secret_name=m2m_client_secret_name,
    gateway_m2m_client_secret=legacy_m2m_client_secret,
    cognito_discovery_url=discovery_url,
    google_client_id=google_client_id,
    google_client_secret_name=google_client_secret_name,
    github_client_id=github_client_id,
    github_client_secret_name=github_client_secret_name,
    notion_client_id=notion_client_id,
    notion_client_secret_name=notion_client_secret_name,
    env=cdk_env,
)
if auth_stack:
    identity_stack.add_dependency(auth_stack)

# ═══════════════════════════════════════════════════════════════
# SERVICE LAYER
# ═══════════════════════════════════════════════════════════════

# ── Memory ──
# Memory is deliberately per-workload in a federation: conversation history is
# the tenant boundary (actor_id), and account isolation is the strongest wall
# available. The platform account runs no agents, so it needs no memory.
memory_stack = None
if not is_fed_platform:
    memory_stack = MemoryStack(
        app,
        f"{prefix}-memory",
        project_name=project,
        environment=env_name,
        kms_key_arn=security_stack.kms_key.key_arn
        if (security_stack and security_stack.kms_key)
        else "",
        event_expiry_days=memory_event_expiry_days,
        use_long_term_memory=use_long_term_memory,
        ltm_top_k=ltm_top_k,
        ltm_relevance_score=ltm_relevance_score,
        enable_resource_policies=enable_resource_policies,
        org_id=org_id,
        retain_data=production_mode,
        env=cdk_env,
    )
    if auth_stack:
        memory_stack.add_dependency(auth_stack)
    if security_stack:
        memory_stack.add_dependency(security_stack)

# ── Gateway (MCP gateway with Lambda tool targets) — platform side only ──
gateway_stack = None
if not is_fed_workload:
    gateway_stack = GatewayStack(
        app,
        f"{prefix}-gateway",
        project_name=project,
        environment=env_name,
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
        allowed_audience=allowed_audience,
        enable_web_search=enable_web_search,
        tool_configs={
            "sample-tool": {
                "source_dir": "tools/sample_tool",
                "env_vars": {},
                "tool_schema": [
                    {
                        "Name": "text_analysis_tool",
                        "Description": "Analyzes text to count words and find most frequent characters.",
                        "InputSchema": {
                            "Type": "object",
                            "Properties": {
                                "text": {
                                    "Type": "string",
                                    "Description": "Input text to analyze",
                                },
                                "N": {
                                    "Type": "integer",
                                    "Description": "Number of most frequent characters to return (default: 5)",
                                },
                            },
                            "Required": ["text"],
                        },
                    },
                ],
            },
        },
        enable_egress_filter=enable_egress_filter,
        enable_cedar=enable_cedar,
        cedar_mode=cedar_mode,
        debug_exceptions=not production_mode,
        kms_key_arn=security_stack.kms_key.key_arn
        if (security_stack and security_stack.kms_key)
        else "",
        env=cdk_env,
    )
    gateway_stack.add_dependency(auth_stack)

# Runtimes reach the gateway at the local stack's URL, or the platform
# account's (from the federation block) in a workload account.
gateway_url_for_runtimes = (
    fed.gateway_url
    if is_fed_workload
    else (gateway_stack.gateway_url if gateway_stack else "")
)

# ═══════════════════════════════════════════════════════════════
# RUNTIME LAYER
# ═══════════════════════════════════════════════════════════════

# ── Runtime network placement ──
# With enable_networking the runtimes belong IN the VPC. Passing nothing here is
# what used to make "enterprise network isolation" false: the VPC and its
# endpoints were built and every agent still ran with networkMode PUBLIC.
runtime_network = (
    {
        "network_mode": "VPC",
        "subnet_ids": networking_stack.private_subnet_ids,
        "security_group_ids": [
            networking_stack.runtime_security_group.security_group_id
        ],
    }
    if networking_stack
    else {}
)

# ── Orchestrator Runtime (HTTP protocol) — not in a federated platform account ──
# Agent pattern selects source directory:
# AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6
runtime_orchestrator = None
if not is_fed_platform:
    runtime_orchestrator = RuntimeStack(
        app,
        f"{prefix}-runtime-orchestrator",
        project_name=project,
        environment=env_name,
        component_name="orchestrator",
        source_dir="agent-code",
        dockerfile_pattern=agent_pattern,
        runtime_type="orchestrator",
        runtime_generation=orchestrator_runtime_generation,
        allowed_models=allowed_models,
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
        allowed_audience=allowed_audience,
        require_guardrails=require_guardrails,
        retain_data=production_mode,
        extra_env_vars={
            "GATEWAY_URL": gateway_url_for_runtimes,
            "GATEWAY_CREDENTIAL_PROVIDER_NAME": identity_stack.gateway_credential_provider_name,
            **gateway_scope_env,
            "MEMORY_ID": memory_stack.memory_id,
            # shared/auth.py verifies the caller's JWT against this issuer's JWKS
            # instead of trusting that the runtime authorizer ran. Without these the
            # agent refuses the request rather than decoding it unverified.
            "COGNITO_ISSUER_URL": issuer_url,
            "COGNITO_ALLOWED_CLIENTS": ",".join(allowed_clients or allowed_audience),
            "STACK_NAME": prefix,
            "USE_LONG_TERM_MEMORY": str(use_long_term_memory).lower(),
            "LTM_TOP_K": str(ltm_top_k),
            "LTM_RELEVANCE_SCORE": str(ltm_relevance_score),
            **model_env,
        },
        **migration_kwargs,
        **runtime_network,
        env=cdk_env,
    )
    if gateway_stack:
        runtime_orchestrator.add_dependency(gateway_stack)
    runtime_orchestrator.add_dependency(memory_stack)
    if networking_stack:
        runtime_orchestrator.add_dependency(networking_stack)
    # The orchestrator fetches Gateway tokens through the identity stack's M2M
    # credential provider, so the provider must exist before the runtime starts.
    # code-agent has no gateway tools; research-agent does (web search), so it
    # gets the same gateway env vars and dependencies below.
    runtime_orchestrator.add_dependency(identity_stack)

# ── A2A Agent Runtimes (optional) ──
runtime_code_agent = None
runtime_research_agent = None

if enable_a2a and not is_fed_platform:
    # Code Agent (A2A protocol)
    runtime_code_agent = RuntimeStack(
        app,
        f"{prefix}-runtime-code-agent",
        project_name=project,
        environment=env_name,
        component_name="code-agent",
        # Build context is agent-code/ (not the pattern dir): the A2A serving
        # helper lives in shared/, so the Dockerfile has to copy it.
        source_dir="agent-code",
        dockerfile_pattern="code-agent",
        runtime_type="a2a_agent",
        allowed_models=allowed_models,
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
        allowed_audience=allowed_audience,
        require_guardrails=require_guardrails,
        retain_data=production_mode,
        extra_env_vars=model_env,
        **runtime_network,
        env=cdk_env,
    )
    if auth_stack:
        runtime_code_agent.add_dependency(auth_stack)
    if networking_stack:
        runtime_code_agent.add_dependency(networking_stack)

    # Research Agent (A2A protocol). Unlike code-agent it has gateway tools
    # (web search), so its build context is agent-code/ (its Dockerfile copies
    # shared/) and it gets the gateway env vars + identity/gateway dependencies.
    runtime_research_agent = RuntimeStack(
        app,
        f"{prefix}-runtime-research-agent",
        project_name=project,
        environment=env_name,
        component_name="research-agent",
        source_dir="agent-code",
        dockerfile_pattern="research-agent",
        runtime_type="a2a_agent",
        allowed_models=allowed_models,
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
        allowed_audience=allowed_audience,
        require_guardrails=require_guardrails,
        retain_data=production_mode,
        extra_env_vars={
            "GATEWAY_URL": gateway_url_for_runtimes,
            "GATEWAY_CREDENTIAL_PROVIDER_NAME": identity_stack.gateway_credential_provider_name,
            **gateway_scope_env,
            **model_env,
        },
        **runtime_network,
        env=cdk_env,
    )
    if auth_stack:
        runtime_research_agent.add_dependency(auth_stack)
    if gateway_stack:
        runtime_research_agent.add_dependency(gateway_stack)
    runtime_research_agent.add_dependency(identity_stack)
    if networking_stack:
        runtime_research_agent.add_dependency(networking_stack)

# ═══════════════════════════════════════════════════════════════
# OBSERVABILITY LAYER
# ═══════════════════════════════════════════════════════════════

# Observability is per-account: each side of a federation monitors what it runs.
monitored_resources = {}
if gateway_stack:
    monitored_resources["gateway"] = gateway_stack.gateway_arn
if memory_stack:
    monitored_resources["memory"] = memory_stack.memory_arn
if runtime_orchestrator:
    # Recovery-only context: pin the currently exported ARN as a literal
    # before a create-only Runtime name change. The resolved value is
    # identical, but removing Observability's cross-stack import lets
    # CloudFormation update the RuntimeArn export during replacement. Omit
    # this context on the immediate follow-up Observability deploy so it binds
    # to the replacement. This is intentionally not an environment or
    # platform.yaml setting: steady state must always consume the live export.
    monitored_resources["runtime-orchestrator"] = (
        app.node.try_get_context("runtime_observability_arn_override")
        or runtime_orchestrator.runtime_arn
    )
if runtime_code_agent:
    monitored_resources["runtime-code-agent"] = runtime_code_agent.runtime_arn
if runtime_research_agent:
    monitored_resources["runtime-research-agent"] = runtime_research_agent.runtime_arn

obs_stack = ObservabilityStack(
    app,
    f"{prefix}-observability",
    project_name=project,
    environment=env_name,
    monitored_resources=monitored_resources,
    orchestrator_runtime_generation=orchestrator_runtime_generation,
    enable_traceability=enable_traceability,
    enable_transaction_search=enable_transaction_search,
    enable_alarms=enable_alarms,
    alarm_email=alarm_email,
    log_retention_days=log_retention_days,
    retain_data=production_mode,
    env=cdk_env,
)
for _dep in (
    runtime_orchestrator,
    gateway_stack,
    memory_stack,
    runtime_code_agent,
    runtime_research_agent,
):
    if _dep:
        obs_stack.add_dependency(_dep)

# ═══════════════════════════════════════════════════════════════
# USE CASES (opt-in product integrations)
# ═══════════════════════════════════════════════════════════════
# Each is a folder under use-cases/ (manifest + stack + verify + walkthrough)
# enabled by naming it in platform.yaml's `use_cases:` block. Its build()
# receives a small context and consumes the platform through the published
# interface (SSM parameters + Cognito tokens — docs/PLATFORM_INTERFACE.md),
# never core stack objects: that is the contract that lets contributions and
# the core evolve independently.
if platform_config and platform_config.use_cases:
    import importlib.util

    from infra_utils.platform_config import USE_CASES_DIR, discover_use_cases

    _manifests = discover_use_cases()
    _uc_ctx = {
        "project": project,
        "environment": env_name,
        "prefix": prefix,
        "ssm_prefix": f"/{project}/{env_name}",
        "region": region,
        "cdk_env": cdk_env,
    }
    for _uc_name in sorted(platform_config.use_cases):
        _manifest = _manifests[_uc_name]
        _entry = USE_CASES_DIR / _uc_name / _manifest.entry
        _spec = importlib.util.spec_from_file_location(
            f"use_case_{_uc_name.replace('-', '_')}", _entry
        )
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.build(app, _uc_ctx, platform_config.use_cases[_uc_name] or {})

# ── Cost-allocation tags ──
# Component = the stack's suffix in the deployment contract (expected_stacks),
# so Cost Explorer can split spend per component instead of per project only.
# Runs after the use-case loop so contributed stacks are tagged too. The tag
# only reaches billing once activated (README "Cost attribution"); Bedrock
# inference itself is untagged — that needs Application Inference Profiles,
# tracked as its own task.
for _stack in app.node.children:
    if isinstance(_stack, cdk.Stack):
        cdk.Tags.of(_stack).add("Component", _stack.node.id.removeprefix(f"{prefix}-"))

app.synth()
