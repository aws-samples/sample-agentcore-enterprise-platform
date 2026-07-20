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

from stacks.auth_stack import AuthStack
from stacks.gateway_stack import GatewayStack
from stacks.identity_stack import IdentityStack
from stacks.memory_stack import MemoryStack
from stacks.networking_stack import NetworkingStack
from stacks.observability_stack import ObservabilityStack
from stacks.runtime_stack import RuntimeStack
from stacks.security_stack import SecurityStack

app = cdk.App()

# ── Configuration (context or environment variables) ──
project = app.node.try_get_context("project") or os.environ.get("PROJECT_NAME", "agentcore-workshop")
env_name = app.node.try_get_context("environment") or os.environ.get("ENVIRONMENT", "dev")
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "us-east-1")
account = os.environ.get("CDK_DEFAULT_ACCOUNT", "")

cdk_env = cdk.Environment(account=account, region=region)
prefix = f"{project}-{env_name}"

# Feature flags
enable_networking = (app.node.try_get_context("enable_networking") or os.environ.get("ENABLE_NETWORKING", "false")) == "true"
enable_security = (app.node.try_get_context("enable_security") or os.environ.get("ENABLE_SECURITY", "false")) == "true"
enable_a2a = (app.node.try_get_context("enable_a2a") or os.environ.get("ENABLE_A2A", "true")) == "true"
idp_type = app.node.try_get_context("idp_type") or os.environ.get("IDP_TYPE", "cognito")
obs_backend = app.node.try_get_context("observability_backend") or os.environ.get("OBSERVABILITY_BACKEND", "cloudwatch")

# Security control feature flags (control-library / scope-split model).
# Additional flags (enable_guardrails, enable_cedar) will be added here as their stacks land.
enable_resource_policies = (app.node.try_get_context("enable_resource_policies") or os.environ.get("ENABLE_RESOURCE_POLICIES", "false")) == "true"
# Egress Lambda interceptor + Bedrock Guardrail on the Gateway (PII masking, prompt injection).
enable_egress_filter = (app.node.try_get_context("enable_egress_filter") or os.environ.get("ENABLE_EGRESS_FILTER", "false")) == "true"
# AgentCore Cedar policy engine on the Gateway (default-forbid on writes). cedar_mode is
# LOG_ONLY (evaluate + log) or ENFORCE (block); ships LOG_ONLY for safe rollout.
enable_cedar = (app.node.try_get_context("enable_cedar") or os.environ.get("ENABLE_CEDAR", "false")) == "true"
cedar_mode = app.node.try_get_context("cedar_mode") or os.environ.get("CEDAR_MODE", "LOG_ONLY")
# Detective controls: SNS + EventBridge alerting on sensitive AgentCore API calls (item 7).
enable_traceability = (app.node.try_get_context("enable_traceability") or os.environ.get("ENABLE_TRACEABILITY", "false")) == "true"
# AWS Organizations ID (o-xxxx). Required when enable_resource_policies is on, so the
# in-account-only resource policies can render their aws:PrincipalOrgID deny guard.
org_id = app.node.try_get_context("org_id") or os.environ.get("ORG_ID", "")

# Agent pattern selection (from FAST reference patterns)
# Options: strands-agent, langgraph-agent, claude-sdk-agent, claude-sdk-multi-agent,
#          agui-strands-agent, agui-langgraph-agent
agent_pattern = app.node.try_get_context("agent_pattern") or os.environ.get("AGENT_PATTERN", "orchestrator")

# Long-term memory configuration
use_long_term_memory = (app.node.try_get_context("use_long_term_memory") or os.environ.get("USE_LONG_TERM_MEMORY", "false")) == "true"
ltm_top_k = int(app.node.try_get_context("ltm_top_k") or os.environ.get("LTM_TOP_K", "10"))
ltm_relevance_score = float(app.node.try_get_context("ltm_relevance_score") or os.environ.get("LTM_RELEVANCE_SCORE", "0.3"))

# IdP config from context or env
idp_config = {
    "tenant_id": app.node.try_get_context("idp_tenant_id") or os.environ.get("IDP_TENANT_ID", ""),
    "client_id": app.node.try_get_context("idp_client_id") or os.environ.get("IDP_CLIENT_ID", ""),
    "client_secret": app.node.try_get_context("idp_client_secret") or os.environ.get("IDP_CLIENT_SECRET", ""),
    "issuer_url": app.node.try_get_context("idp_issuer_url") or os.environ.get("IDP_ISSUER_URL", ""),
}

# OAuth provider credentials
google_client_id = app.node.try_get_context("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID", "")
google_client_secret = app.node.try_get_context("google_client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
github_client_id = app.node.try_get_context("github_client_id") or os.environ.get("GITHUB_CLIENT_ID", "")
github_client_secret = app.node.try_get_context("github_client_secret") or os.environ.get("GITHUB_CLIENT_SECRET", "")
notion_client_id = app.node.try_get_context("notion_client_id") or os.environ.get("NOTION_CLIENT_ID", "")
notion_client_secret = app.node.try_get_context("notion_client_secret") or os.environ.get("NOTION_CLIENT_SECRET", "")

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
    networking_stack = NetworkingStack(app, f"{prefix}-networking",
        project_name=project, environment=env_name,
        enable_vpc_endpoints=True,
        org_id=org_id,
        env=cdk_env)

# ── Optional: Security (KMS CMK, CloudTrail) ──
security_stack = None
if enable_security:
    security_stack = SecurityStack(app, f"{prefix}-security",
        project_name=project, environment=env_name,
        enable_kms=True, enable_cloudtrail=True,
        env=cdk_env)

# ═══════════════════════════════════════════════════════════════
# IDENTITY LAYER
# ═══════════════════════════════════════════════════════════════

# ── Auth (Cognito + federated IdP) ──
auth_stack = AuthStack(app, f"{prefix}-auth",
    project_name=project, environment=env_name,
    idp_type=idp_type, idp_config=idp_config,
    env=cdk_env)

# ── Identity (3LO OAuth providers) ──
identity_stack = IdentityStack(app, f"{prefix}-identity",
    project_name=project, environment=env_name,
    google_client_id=google_client_id, google_client_secret=google_client_secret,
    github_client_id=github_client_id, github_client_secret=github_client_secret,
    notion_client_id=notion_client_id, notion_client_secret=notion_client_secret,
    env=cdk_env)
identity_stack.add_dependency(auth_stack)

# ═══════════════════════════════════════════════════════════════
# SERVICE LAYER
# ═══════════════════════════════════════════════════════════════

# ── Memory ──
memory_stack = MemoryStack(app, f"{prefix}-memory",
    project_name=project, environment=env_name,
    kms_key_arn=security_stack.kms_key.key_arn if (security_stack and security_stack.kms_key) else "",
    event_expiry_days=30,
    use_long_term_memory=use_long_term_memory,
    ltm_top_k=ltm_top_k,
    ltm_relevance_score=ltm_relevance_score,
    enable_resource_policies=enable_resource_policies,
    org_id=org_id,
    env=cdk_env)
memory_stack.add_dependency(auth_stack)
if security_stack:
    memory_stack.add_dependency(security_stack)

# ── Gateway (MCP gateway with Lambda tool targets) ──
gateway_stack = GatewayStack(app, f"{prefix}-gateway",
    project_name=project, environment=env_name,
    cognito_issuer_url=auth_stack.issuer_url,
    cognito_allowed_clients=[auth_stack.app_client_id, auth_stack.m2m_client_id],
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
                            "text": {"Type": "string", "Description": "Input text to analyze"},
                            "N": {"Type": "integer", "Description": "Number of most frequent characters to return (default: 5)"},
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
    env=cdk_env)
gateway_stack.add_dependency(auth_stack)

# ═══════════════════════════════════════════════════════════════
# RUNTIME LAYER
# ═══════════════════════════════════════════════════════════════

# ── Orchestrator Runtime (HTTP protocol) ──
# Agent pattern selects source directory: cdk deploy -c agent_pattern=langgraph-agent
runtime_orchestrator = RuntimeStack(app, f"{prefix}-runtime-orchestrator",
    project_name=project, environment=env_name,
    component_name="orchestrator",
    source_dir="agent-code",
    dockerfile_pattern=agent_pattern,
    runtime_type="orchestrator",
    cognito_issuer_url=auth_stack.issuer_url,
    cognito_allowed_clients=[auth_stack.app_client_id, auth_stack.m2m_client_id],
    extra_env_vars={
        "GATEWAY_URL": gateway_stack.gateway_url,
        "MEMORY_ID": memory_stack.memory_id,
        "STACK_NAME": prefix,
        "USE_LONG_TERM_MEMORY": str(use_long_term_memory).lower(),
        "LTM_TOP_K": str(ltm_top_k),
        "LTM_RELEVANCE_SCORE": str(ltm_relevance_score),
    },
    env=cdk_env)
runtime_orchestrator.add_dependency(gateway_stack)
runtime_orchestrator.add_dependency(memory_stack)

# ── A2A Agent Runtimes (optional) ──
runtime_code_agent = None
runtime_research_agent = None

if enable_a2a:
    # Code Agent (A2A protocol)
    runtime_code_agent = RuntimeStack(app, f"{prefix}-runtime-code-agent",
        project_name=project, environment=env_name,
        component_name="code-agent",
        source_dir="agent-code/code-agent",
        runtime_type="a2a_agent",
        cognito_issuer_url=auth_stack.issuer_url,
        cognito_allowed_clients=[auth_stack.app_client_id, auth_stack.m2m_client_id],
        env=cdk_env)
    runtime_code_agent.add_dependency(auth_stack)

    # Research Agent (A2A protocol)
    runtime_research_agent = RuntimeStack(app, f"{prefix}-runtime-research-agent",
        project_name=project, environment=env_name,
        component_name="research-agent",
        source_dir="agent-code/research-agent",
        runtime_type="a2a_agent",
        cognito_issuer_url=auth_stack.issuer_url,
        cognito_allowed_clients=[auth_stack.app_client_id, auth_stack.m2m_client_id],
        env=cdk_env)
    runtime_research_agent.add_dependency(auth_stack)

# ═══════════════════════════════════════════════════════════════
# OBSERVABILITY LAYER
# ═══════════════════════════════════════════════════════════════

monitored_resources = {
    "gateway": gateway_stack.gateway_arn,
    "memory": memory_stack.memory_arn,
    "runtime-orchestrator": runtime_orchestrator.runtime_arn,
}
if runtime_code_agent:
    monitored_resources["runtime-code-agent"] = runtime_code_agent.runtime_arn
if runtime_research_agent:
    monitored_resources["runtime-research-agent"] = runtime_research_agent.runtime_arn

obs_stack = ObservabilityStack(app, f"{prefix}-observability",
    project_name=project, environment=env_name,
    backend=obs_backend,
    monitored_resources=monitored_resources,
    enable_traceability=enable_traceability,
    env=cdk_env)
obs_stack.add_dependency(runtime_orchestrator)
obs_stack.add_dependency(gateway_stack)
obs_stack.add_dependency(memory_stack)
if runtime_code_agent:
    obs_stack.add_dependency(runtime_code_agent)
if runtime_research_agent:
    obs_stack.add_dependency(runtime_research_agent)

app.synth()
