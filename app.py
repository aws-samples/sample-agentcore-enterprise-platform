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

Deploy individual stacks:
    cdk deploy agentcore-workshop-dev-auth
    cdk deploy agentcore-workshop-dev-gateway
    cdk deploy --all
"""

import os

import aws_cdk as cdk

from infra_utils.platform_config import PlatformConfig, load_platform_config, to_env
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
    platform_config = load_platform_config(_config_path)
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


# Security control feature flags (control-library / scope-split model).
# Additional flags (enable_guardrails, enable_cedar) will be added here as their stacks land.
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
# AWS Organizations ID (o-xxxx). Required when enable_resource_policies is on, so the
# in-account-only resource policies can render their aws:PrincipalOrgID deny guard.
org_id = cfg("org_id", "ORG_ID", "")

# Agent pattern selection (from FAST reference patterns)
# Options: strands-agent, langgraph-agent, claude-sdk-agent, claude-sdk-multi-agent,
#          agui-strands-agent, agui-langgraph-agent
agent_pattern = cfg("agent_pattern", "AGENT_PATTERN", "orchestrator")

# Optional Bedrock model ID override (cross-region inference profile, e.g.
# us.anthropic.claude-sonnet-5). When unset, MODEL_ID is NOT injected into the
# runtimes and each agent pattern falls back to its in-code DEFAULT_MODEL_ID —
# agent code stays the single source of truth for the default.
model_id = cfg("model_id", "MODEL_ID", "")
model_env = {"MODEL_ID": model_id} if model_id else {}

# Long-term memory configuration
use_long_term_memory = (
    cfg("use_long_term_memory", "USE_LONG_TERM_MEMORY", "false") == "true"
)
ltm_top_k = int(cfg("ltm_top_k", "LTM_TOP_K", "10"))
ltm_relevance_score = float(cfg("ltm_relevance_score", "LTM_RELEVANCE_SCORE", "0.3"))

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
        idp_config=idp_config,
        env=cdk_env,
    )

# Where runtimes/gateway find the issuer and M2M client: the local auth stack,
# or the platform account's (via the federation block) in a workload account.
if is_fed_workload:
    issuer_url = fed.issuer_url
    discovery_url = fed.discovery_url
    m2m_client_id = fed.m2m_client_id
    m2m_client_secret = cdk.SecretValue.secrets_manager(fed.m2m_client_secret_name)
    allowed_clients = [fed.m2m_client_id]
else:
    issuer_url = auth_stack.issuer_url
    discovery_url = auth_stack.discovery_url
    m2m_client_id = auth_stack.m2m_client_id
    m2m_client_secret = auth_stack.m2m_client_secret
    allowed_clients = [auth_stack.app_client_id, auth_stack.m2m_client_id]

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
    gateway_m2m_client_secret=m2m_client_secret,
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
        event_expiry_days=30,
        use_long_term_memory=use_long_term_memory,
        ltm_top_k=ltm_top_k,
        ltm_relevance_score=ltm_relevance_score,
        enable_resource_policies=enable_resource_policies,
        org_id=org_id,
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
# Agent pattern selects source directory: cdk deploy -c agent_pattern=langgraph-agent
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
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
        extra_env_vars={
            "GATEWAY_URL": gateway_url_for_runtimes,
            "GATEWAY_CREDENTIAL_PROVIDER_NAME": identity_stack.gateway_credential_provider_name,
            "MEMORY_ID": memory_stack.memory_id,
            # shared/auth.py verifies the caller's JWT against this issuer's JWKS
            # instead of trusting that the runtime authorizer ran. Without these the
            # agent refuses the request rather than decoding it unverified.
            "COGNITO_ISSUER_URL": issuer_url,
            "COGNITO_ALLOWED_CLIENTS": ",".join(allowed_clients),
            "STACK_NAME": prefix,
            "USE_LONG_TERM_MEMORY": str(use_long_term_memory).lower(),
            "LTM_TOP_K": str(ltm_top_k),
            "LTM_RELEVANCE_SCORE": str(ltm_relevance_score),
            **model_env,
        },
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
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
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
        cognito_issuer_url=issuer_url,
        cognito_allowed_clients=allowed_clients,
        extra_env_vars={
            "GATEWAY_URL": gateway_url_for_runtimes,
            "GATEWAY_CREDENTIAL_PROVIDER_NAME": identity_stack.gateway_credential_provider_name,
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
    monitored_resources["runtime-orchestrator"] = runtime_orchestrator.runtime_arn
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
    enable_traceability=enable_traceability,
    enable_transaction_search=enable_transaction_search,
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

app.synth()
