#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════
# AgentCore Workshop Simulation — Progressive Deployment
# ═══════════════════════════════════════════════════════════════
# Simulates a customer workshop: destroy existing → rebuild step by step
# Each module pauses for explanation before deploying.
#
# Usage:
#   ./run-workshop.sh              # Full simulation
#   ./run-workshop.sh --module 3   # Start from specific module
#   ./run-workshop.sh --destroy    # Destroy all stacks
# ═══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log_info()   { echo -e "${GREEN}✓${NC} $*"; }
log_step()   { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${NC}\n"; }
log_action() { echo -e "${BLUE}▶${NC} $*"; }
log_explain(){ echo -e "${YELLOW}📖${NC} $*"; }
log_error()  { echo -e "${RED}✗${NC} $*"; }

pause() {
    echo ""
    echo -e "${BOLD}Press ENTER to continue to next step...${NC}"
    read -r
}

# ── Configuration ──
export PROJECT_NAME="agentcore-workshop"
export ENVIRONMENT="dev"
PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"

# EntraID config — set these to YOUR Entra ID app registration before running:
#   export IDP_TENANT_ID='<your-entra-tenant-id>'
#   export IDP_CLIENT_ID='<your-entra-app-client-id>'
#   export IDP_CLIENT_SECRET='<your-entra-client-secret>'
export IDP_TYPE="${IDP_TYPE:-entra_id}"

# Feature flags — start minimal, enable progressively
export ENABLE_NETWORKING="false"
export ENABLE_SECURITY="false"
export ENABLE_A2A="false"
export AGENT_PATTERN="strands-agent"

START_MODULE="${1:-1}"
if [[ "${1:-}" == "--destroy" ]]; then
    log_step "DESTROYING ALL STACKS"
    cd "$PROJECT_DIR"
    cdk destroy --all --force
    log_info "All stacks destroyed."
    exit 0
fi

if [[ "${1:-}" == "--module" ]]; then
    START_MODULE="${2:-1}"
fi

# ═══════════════════════════════════════════════════════════════
# PRE-FLIGHT
# ═══════════════════════════════════════════════════════════════

log_step "PRE-FLIGHT CHECKS"

# Trim the client secret before validating it. A value pasted from the Entra
# portal, or piped in from `az ad app credential reset -o tsv`, carries a
# trailing newline; Cognito forwards it verbatim to the IdP and the token
# exchange fails with invalid_client, naming nothing about whitespace (see
# docs/ENTERPRISE_IDP.md). A whitespace-only value trims to empty and is caught
# by the missing-vars check below.
IDP_CLIENT_SECRET="$(printf '%s' "${IDP_CLIENT_SECRET:-}" | tr -d '\n\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

MISSING_IDP_VARS=()
[[ -z "${IDP_TENANT_ID:-}" ]] && MISSING_IDP_VARS+=("IDP_TENANT_ID")
[[ -z "${IDP_CLIENT_ID:-}" ]] && MISSING_IDP_VARS+=("IDP_CLIENT_ID")
[[ -z "${IDP_CLIENT_SECRET:-}" ]] && MISSING_IDP_VARS+=("IDP_CLIENT_SECRET")
if [[ ${#MISSING_IDP_VARS[@]} -gt 0 ]]; then
    log_error "Missing IdP configuration: ${MISSING_IDP_VARS[*]}"
    echo "  Export your Entra ID app registration values before running:"
    echo "    export IDP_TENANT_ID='<your-entra-tenant-id>'"
    echo "    export IDP_CLIENT_ID='<your-entra-app-client-id>'"
    echo "    export IDP_CLIENT_SECRET='<your-entra-client-secret>'"
    exit 1
fi

cd "$PROJECT_DIR"

# Verify AWS identity
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
if [[ -z "$ACCOUNT_ID" ]]; then
    log_error "AWS credentials not configured"
    exit 1
fi
export CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID"
export CDK_DEFAULT_REGION="${AWS_REGION:-us-east-1}"

# Store the IdP client secret in Secrets Manager. CDK only ever receives the
# secret NAME — the auth stack resolves the value at deploy time via a
# {{resolve:secretsmanager:...}} dynamic reference, so the plaintext never
# appears in process listings or the synthesized template.
IDP_SECRET_NAME="${PREFIX}-idp-client-secret"
if ! aws secretsmanager create-secret --name "$IDP_SECRET_NAME" \
    --secret-string "$IDP_CLIENT_SECRET" --region "$CDK_DEFAULT_REGION" &>/dev/null; then
    # Secret already exists — update the value (IdP secrets rotate).
    aws secretsmanager put-secret-value --secret-id "$IDP_SECRET_NAME" \
        --secret-string "$IDP_CLIENT_SECRET" --region "$CDK_DEFAULT_REGION" >/dev/null
fi
log_info "IdP client secret stored in Secrets Manager: $IDP_SECRET_NAME"

log_info "AWS Account: $ACCOUNT_ID"
log_info "Region: $CDK_DEFAULT_REGION"
log_info "EntraID Tenant: $IDP_TENANT_ID"
log_info "Agent Pattern: $AGENT_PATTERN"
echo ""

# ═══════════════════════════════════════════════════════════════
# MODULE 0: DESTROY EXISTING
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 0 ]]; then
    log_step "MODULE 0: Clean Slate — Destroying Existing Deployment"
    log_explain "Customer scenario: We're starting fresh. Tearing down any previous deployment."
    log_action "Running: cdk destroy --all --force"
    pause

    cdk destroy --all --force 2>&1 | tail -20
    log_info "Clean slate achieved."
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 1: INFRASTRUCTURE — Auth + EntraID Federation
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 1 ]]; then
    log_step "MODULE 1: Infrastructure Blueprint — Cognito + EntraID Federation"
    echo ""
    log_explain "What we're building:"
    echo "  • Cognito User Pool with email sign-in"
    echo "  • Federated OIDC provider → Microsoft Entra ID"
    echo "  • Resource server with 'agentcore/invoke' scope"
    echo "  • 3 OAuth clients: app (auth code), web (SRP), m2m (client_credentials)"
    echo "  • SSM parameters for cross-stack discovery"
    echo ""
    log_explain "Why EntraID?"
    echo "  ACME Corp uses Microsoft 365. Their employees already have Entra accounts."
    echo "  We federate Cognito → Entra so users log in with their corporate credentials."
    echo "  The Cognito token wraps the Entra identity for AgentCore consumption."
    echo ""
    log_action "Deploying: ${PREFIX}-auth"
    log_action "Command: cdk deploy ${PREFIX}-auth -c idp_type=entra_id -c idp_tenant_id=\$IDP_TENANT_ID ..."
    pause

    cdk deploy "${PREFIX}-auth" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        --require-approval never 2>&1 | tail -30

    log_info "Auth stack deployed with EntraID federation."
    echo ""
    log_explain "Verify: Check Cognito console → User Pool → Sign-in experience → Federated identity providers"
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 2: IDENTITY — 3LO OAuth Credential Providers
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 2 ]]; then
    log_step "MODULE 2: Identity Integration — OAuth Credential Providers"
    echo ""
    log_explain "What we're building:"
    echo "  • AgentCore OAuth2 Credential Providers for 3LO delegation"
    echo "  • These let agents act on behalf of users with external services"
    echo "  • Example: Agent reads user's Google Calendar or GitHub repos"
    echo ""
    log_explain "For this demo, we skip external OAuth providers (no Google/GitHub keys)."
    echo "  The stack deploys but creates no providers (conditional on client_id)."
    echo ""
    log_action "Deploying: ${PREFIX}-identity"
    pause

    cdk deploy "${PREFIX}-identity" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        --require-approval never 2>&1 | tail -20

    log_info "Identity stack deployed."
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 3: GATEWAY — MCP Gateway + Lambda Tools
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 3 ]]; then
    log_step "MODULE 3: Gateway & Tool Registry"
    echo ""
    log_explain "What we're building:"
    echo "  • AgentCore MCP Gateway (managed API for tool access)"
    echo "  • Lambda-backed tool target (text_analysis_tool)"
    echo "  • CUSTOM_JWT auth using Cognito issuer"
    echo ""
    log_explain "Why a Gateway?"
    echo "  • Centralized tool access control (who can call what)"
    echo "  • Protocol translation (MCP ↔ Lambda, HTTP, etc.)"
    echo "  • Audit trail for all tool invocations"
    echo "  • Agents discover tools via the gateway — no hardcoded endpoints"
    echo ""
    log_action "Deploying: ${PREFIX}-gateway"
    pause

    cdk deploy "${PREFIX}-gateway" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        --require-approval never 2>&1 | tail -30

    log_info "Gateway deployed with sample tool target."
    echo ""
    log_explain "The gateway URL is stored in SSM: /${PROJECT_NAME}/${ENVIRONMENT}/gateway/url"
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 4: MEMORY
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 4 ]]; then
    log_step "MODULE 4: Memory — Semantic + User Preference"
    echo ""
    log_explain "What we're building:"
    echo "  • AgentCore Memory resource (managed conversation memory)"
    echo "  • Semantic memory strategy (vector-based recall)"
    echo "  • User preference strategy (learns user patterns)"
    echo ""
    log_explain "Why managed memory?"
    echo "  ACME's EC2 agent had no persistent memory — every conversation started fresh."
    echo "  AgentCore Memory gives agents context across sessions without custom infra."
    echo ""
    log_action "Deploying: ${PREFIX}-memory"
    pause

    cdk deploy "${PREFIX}-memory" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        --require-approval never 2>&1 | tail -20

    log_info "Memory stack deployed."
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 5: AGENT MIGRATION — EC2 → AgentCore Runtime
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 5 ]]; then
    log_step "MODULE 5: Agent Migration — EC2 → AgentCore Runtime"
    echo ""
    log_explain "The migration story:"
    echo "  BEFORE: Agent runs on EC2"
    echo "    • Manual scaling, patching, monitoring"
    echo "    • No auth integration"
    echo "    • No tool governance"
    echo "    • No memory persistence"
    echo ""
    echo "  AFTER: Agent runs on AgentCore Runtime"
    echo "    • Managed compute (container-based, auto-scaling)"
    echo "    • Cognito/EntraID auth built-in"
    echo "    • Gateway for tool access"
    echo "    • Managed memory"
    echo ""
    log_explain "What changes in the code?"
    echo "  1. Add Dockerfile (containerize the agent)"
    echo "  2. Add agentcore.toml (runtime config)"
    echo "  3. Wire gateway URL + memory ID via env vars"
    echo "  4. That's it — the agent logic stays the same"
    echo ""
    echo "  See: workshop-simulation/existing-ec2-agent/agent.py (before)"
    echo "  See: agent-code/strands-agent/agent.py (after)"
    echo ""
    log_action "Deploying: ${PREFIX}-runtime-orchestrator (pattern: strands-agent)"
    pause

    cdk deploy "${PREFIX}-runtime-orchestrator" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        -c agent_pattern="strands-agent" \
        --require-approval never 2>&1 | tail -30

    log_info "Orchestrator runtime deployed (strands-agent pattern)."
    echo ""
    log_explain "The agent is now running on AgentCore Runtime with:"
    echo "  • Cognito + EntraID auth"
    echo "  • Gateway tool access"
    echo "  • Managed memory"
    echo "  • Container-based compute"
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 6: A2A — Agent-to-Agent Communication
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 6 ]]; then
    log_step "MODULE 6: Agent-to-Agent (A2A) Communication"
    echo ""
    log_explain "What we're building:"
    echo "  • Code Agent — specialized for code generation/review"
    echo "  • Research Agent — specialized for web research"
    echo "  • Both register as A2A agents that the orchestrator can delegate to"
    echo ""
    log_explain "Why A2A?"
    echo "  Instead of one monolithic agent, ACME splits capabilities:"
    echo "  • Orchestrator decides what to do"
    echo "  • Delegates code tasks → Code Agent"
    echo "  • Delegates research → Research Agent"
    echo "  • Each agent has its own auth, scaling, and lifecycle"
    echo ""
    log_action "Enabling A2A and deploying sub-agents"
    pause

    export ENABLE_A2A="true"

    cdk deploy "${PREFIX}-runtime-code-agent" "${PREFIX}-runtime-research-agent" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        -c enable_a2a=true \
        --require-approval never 2>&1 | tail -30

    log_info "A2A agents deployed (code-agent + research-agent)."
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 7: SECURITY — KMS + CloudTrail
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 7 ]]; then
    log_step "MODULE 7: Security Hardening"
    echo ""
    log_explain "What we're adding:"
    echo "  • KMS Customer Managed Key (CMK) for encryption at rest"
    echo "  • CloudTrail for API audit logging"
    echo ""
    log_explain "Enterprise requirement:"
    echo "  ACME's security team requires all data encrypted with customer-managed keys"
    echo "  and full audit trail of all AgentCore API calls."
    echo ""
    log_action "Deploying: ${PREFIX}-security"
    pause

    export ENABLE_SECURITY="true"

    cdk deploy "${PREFIX}-security" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        -c enable_security=true \
        --require-approval never 2>&1 | tail -20

    log_info "Security stack deployed (KMS + CloudTrail)."
    pause
fi

# ═══════════════════════════════════════════════════════════════
# MODULE 8: OBSERVABILITY
# ═══════════════════════════════════════════════════════════════

if [[ "$START_MODULE" -le 8 ]]; then
    log_step "MODULE 8: Observability"
    echo ""
    log_explain "What we're adding:"
    echo "  • Vended log delivery for all AgentCore resources"
    echo "  • X-Ray tracing for request flow visibility"
    echo "  • Per-resource monitoring (gateway, memory, runtimes)"
    echo ""
    log_action "Deploying: ${PREFIX}-observability"
    pause

    cdk deploy "${PREFIX}-observability" \
        -c idp_type="$IDP_TYPE" \
        -c idp_tenant_id="$IDP_TENANT_ID" \
        -c idp_client_id="$IDP_CLIENT_ID" \
        -c idp_client_secret_name="$IDP_SECRET_NAME" \
        -c enable_a2a=true \
        --require-approval never 2>&1 | tail -20

    log_info "Observability stack deployed."
    pause
fi

# ═══════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════

log_step "WORKSHOP COMPLETE 🎉"
echo ""
echo "What was deployed:"
echo "  ✓ Cognito User Pool + EntraID federation"
echo "  ✓ OAuth credential providers (identity)"
echo "  ✓ MCP Gateway with Lambda tool targets"
echo "  ✓ Managed Memory (semantic + user preference)"
echo "  ✓ Orchestrator Runtime (migrated from EC2)"
echo "  ✓ A2A sub-agents (code + research)"
echo "  ✓ Security (KMS + CloudTrail)"
echo "  ✓ Observability (vended logs + X-Ray)"
echo ""
echo "Key SSM parameters:"
echo "  /${PROJECT_NAME}/${ENVIRONMENT}/auth/issuer-url"
echo "  /${PROJECT_NAME}/${ENVIRONMENT}/gateway/url"
echo "  /${PROJECT_NAME}/${ENVIRONMENT}/memory/memory-id"
echo "  /${PROJECT_NAME}/${ENVIRONMENT}/runtimes/orchestrator/arn"
echo ""
echo "Next steps:"
echo "  • Test auth: ./scripts/test_agent.py"
echo "  • Test gateway: ./scripts/test_gateway.py"
echo "  • Test memory: ./scripts/test_memory.py"
echo "  • View dashboard: cd dashboard && python3 -m http.server 8888 -d public"
