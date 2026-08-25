#!/usr/bin/env bash
set -euo pipefail

# ── Bash version check (must run before any bash-4 syntax like `declare -A`) ──
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    echo "ERROR: This script requires bash 4 or newer (you are running bash ${BASH_VERSION})." >&2
    echo "" >&2
    echo "macOS ships bash 3.2 as /bin/bash. To fix:" >&2
    echo "  1. brew install bash" >&2
    echo "  2. Run the script with the new bash explicitly:" >&2
    echo "       bash scripts/deploy.sh deploy" >&2
    echo "     (or open a new terminal / run 'hash -r' so your PATH picks up" >&2
    echo "      the Homebrew bash at /opt/homebrew/bin/bash or /usr/local/bin/bash)" >&2
    exit 1
fi

# ═══════════════════════════════════════════════════════════════
# AgentCore Workshop CDK Deploy Script
# ═══════════════════════════════════════════════════════════════
#
# Usage:
#   ./deploy.sh deploy [--stack STACK] [--profile PROFILE] [--team TEAM] [--module N]
#   ./deploy.sh workshop [--profile PROFILE] [--from MODULE] [--dry-run]
#   ./deploy.sh destroy [--stack STACK]
#   ./deploy.sh synth
#   ./deploy.sh diff
#   ./deploy.sh export
#   ./deploy.sh config [--reset]
#
# Profiles: greenfield, migration, multi-agent, platform-team, security-focused
# Teams: platform, agent, security
# Modules: 3, 4, 5, 6, 7, 8, 9, A, B, C, D, E
# ═══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC}  $*"; }
log_header(){ echo -e "\n${BOLD}${CYAN}═══ $* ═══${NC}\n"; }
log_explain(){ echo -e "${YELLOW}📖${NC} $*"; }

# ═══════════════════════════════════════════════════════════════
# Saved Workshop Config (workshop.env)
# ═══════════════════════════════════════════════════════════════
# Precedence: env var > saved workshop.env > default.
# Secrets (IDP_CLIENT_SECRET, API keys) are NEVER persisted —
# the IdP secret lives in Secrets Manager (see upsert_idp_secret).
# ponytail: flat sourceable KEY=value file; upgrade path is the
# declarative Pydantic/YAML config task on the board.
CONFIG_FILE="$PROJECT_DIR/workshop.env"
CONFIG_KEYS=(AWS_REGION IDP_TYPE IDP_TENANT_ID IDP_CLIENT_ID IDP_ISSUER_URL
             MODEL_ID ORG_ID PROJECT_NAME ENVIRONMENT AGENT_PATTERN)

save_config() {
    # CI runs (NON_INTERACTIVE=1) never write the file.
    [ "${NON_INTERACTIVE:-0}" = "1" ] && return 0
    local key
    {
        echo "# AgentCore workshop saved answers — safe to edit or delete. Secrets are never stored here."
        for key in "${CONFIG_KEYS[@]}"; do
            [ -z "${!key:-}" ] && continue
            printf '%s=%q\n' "$key" "${!key}"
        done
    } > "$CONFIG_FILE"
    log_info "Saved answers to $CONFIG_FILE"
}

load_config() {
    [ -f "$CONFIG_FILE" ] || return 0
    local key n
    declare -A pre_load
    for key in "${CONFIG_KEYS[@]}"; do pre_load[$key]="${!key:-}"; done
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
    # Env vars and explicit exports always win over the saved file.
    for key in "${CONFIG_KEYS[@]}"; do
        [ -n "${pre_load[$key]}" ] && printf -v "$key" '%s' "${pre_load[$key]}"
    done
    n=$(grep -c '=' "$CONFIG_FILE" || true)
    log_info "Loaded saved config from workshop.env ($n values; env vars override)"
}
# ── platform.yaml (declarative config) ──
# Precedence: explicit env vars > platform.yaml > workshop.env (wizard answers)
# > interactive prompts. A user-authored file beats remembered answers, so it
# is applied BEFORE load_config (which only fills still-unset keys). Fail-soft
# here: this runs before setup_venv, so pydantic may not exist yet — app.py
# hard-validates the same file at synth time either way.
PLATFORM_CONFIG="${PLATFORM_CONFIG:-$PROJECT_DIR/platform.yaml}"
apply_platform_config() {
    [ -f "$PLATFORM_CONFIG" ] || return 0
    local py="$PROJECT_DIR/.venv/bin/python"
    [ -x "$py" ] || py="python3"
    local exports key value
    # cd: infra_utils must be importable; this runs before the main-flow cd.
    if ! exports=$(cd "$PROJECT_DIR" && "$py" -m infra_utils.platform_config --export "$PLATFORM_CONFIG" 2>&1); then
        log_warn "platform.yaml present but not loadable yet (missing venv?) — continuing without it."
        log_warn "Validate it with: python -m infra_utils.platform_config $PLATFORM_CONFIG"
        # A malformed file must not silently deploy defaults: hard-stop when the
        # parse failed for a reason other than missing dependencies.
        if ! echo "$exports" | grep -q "ModuleNotFoundError"; then
            log_error "platform.yaml is invalid:"
            echo "$exports" >&2
            exit 1
        fi
        return 0
    fi
    local applied=0
    while IFS='=' read -r key value; do
        [ -z "$key" ] && continue
        if [ -z "${!key:-}" ]; then
            printf -v "$key" '%s' "$value"
            export "${key?}"
            applied=$((applied + 1))
        fi
    done <<< "$exports"
    log_info "Applied $applied value(s) from $PLATFORM_CONFIG (env vars override)"
}
apply_platform_config
load_config

# ── Configuration ──
PROJECT_NAME="${PROJECT_NAME:-agentcore-workshop}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"

# ── Agent Pattern ──
# Selects which Dockerfile under agent-code/ the runtime builds (app.py reads
# it as the agent_pattern context value). Validated here because a typo would
# otherwise surface minutes later as a CodeBuild failure on a missing path.
AGENT_PATTERNS="orchestrator strands-agent langgraph-agent claude-sdk-agent claude-sdk-multi-agent agui-strands-agent agui-langgraph-agent"
AGENT_PATTERN="${AGENT_PATTERN:-orchestrator}"
case " $AGENT_PATTERNS " in
    *" $AGENT_PATTERN "*) ;;
    *) log_error "Unknown agent pattern: '$AGENT_PATTERN'. Valid patterns: $AGENT_PATTERNS"
       exit 1 ;;
esac

# ═══════════════════════════════════════════════════════════════
# Workshop Module → CDK Stack Mapping (Requirement 19)
# ═══════════════════════════════════════════════════════════════
declare -A MODULE_MAP
MODULE_MAP[3]="${PREFIX}-auth"                                                    # Infrastructure Blueprint
MODULE_MAP[4]="${PREFIX}-auth ${PREFIX}-identity"                                 # Identity Integration
MODULE_MAP[5]="${PREFIX}-gateway"                                                 # Gateway & Registry
MODULE_MAP[6]="${PREFIX}-runtime-orchestrator"                                    # Agent Deployment
MODULE_MAP[7]="${PREFIX}-gateway"                                                 # Gateway Integration (add targets)
MODULE_MAP[8]="${PREFIX}-runtime-code-agent ${PREFIX}-runtime-research-agent"     # A2A
MODULE_MAP[9]="${PREFIX}-observability"                                           # Observability
MODULE_MAP[A]="${PREFIX}-memory"                                                  # Memory
MODULE_MAP[B]="${PREFIX}-runtime-orchestrator"                                    # Code Interpreter
MODULE_MAP[C]="${PREFIX}-networking"                                              # Multi-Account Mesh (deploys the networking foundation)
# Module D (CI/CD Pipeline) is a guided module with no CDK stacks — see the
# --module D handling below and .gitlab-ci.yml for the reference implementation.
MODULE_MAP[E]="${PREFIX}-security"                                                # Security Automation
VALID_MODULES="3 4 5 6 7 8 9 A B C D E"

# ── Guided workshop narration (workshop action) ──
# First line of each entry is the module title; the rest is the what/why
# explanation shown before deploying. One physical line per entry (with \n
# escapes) so check-workshop-flow.sh can extract them with sed.
declare -A MODULE_EXPLAIN
MODULE_EXPLAIN[3]=$'Infrastructure Blueprint\nWhat: Cognito User Pool with email sign-in, OAuth clients (app/web/m2m), and SSM parameters for cross-stack discovery.\nWhy: every AgentCore call is authenticated — this identity foundation is the trust root everything else builds on.'
MODULE_EXPLAIN[4]=$'Identity Integration\nWhat: the AgentCore M2M credential provider agents use to reach the Gateway. Optional on top: your enterprise IdP federated into Cognito (set IDP_TYPE=entra_id|okta|ping) and 3LO providers for Google/GitHub/Notion (only when you supply their client ids). Walkthrough: docs/ENTERPRISE_IDP.md\nWhy: users keep their corporate logins, and agents get scoped credentials to act on their behalf with external services.'
MODULE_EXPLAIN[5]=$'Gateway & Registry\nWhat: AgentCore MCP Gateway with Lambda tool targets and CUSTOM_JWT auth against the Cognito issuer.\nWhy: centralized, governed tool access — agents discover tools through the gateway instead of hardcoding endpoints, with an audit trail for every call.'
MODULE_EXPLAIN[6]=$'Agent Deployment\nWhat: the orchestrator agent on AgentCore Runtime — CodeBuild builds the container remotely (no local Docker) and CfnRuntime runs it.\nWhy: managed, auto-scaling agent compute with auth wired in; no servers to patch, the agent code stays the same.'
MODULE_EXPLAIN[7]=$'Gateway Integration\nWhat: adds/updates tool targets by redeploying the gateway stack — built-in connectors (no code) or your own Lambda tools. Walkthrough: docs/GATEWAY_TARGETS.md\nWhy: this is how a real platform grows — new tools land in the registry and agents pick them up on the next discovery, with no agent redeploy.'
MODULE_EXPLAIN[8]=$'Agent-to-Agent (A2A)\nWhat: code + research sub-agents on their own Runtimes; the orchestrator delegates specialized tasks to them over A2A.\nWhy: specialized agents with independent auth, scaling, and lifecycle beat one monolith. (Requires enable_a2a=true — set automatically for this module.)'
MODULE_EXPLAIN[9]=$'Observability\nWhat: vended log delivery + X-Ray tracing for the gateway, memory, and runtimes.\nWhy: you cannot operate what you cannot see — per-resource logs and end-to-end request traces.'
MODULE_EXPLAIN[A]=$'Memory\nWhat: AgentCore managed Memory with a user-preference strategy (semantic fact extraction is added only when USE_LONG_TERM_MEMORY=true — it costs more).\nWhy: agents keep context across sessions without building custom vector infrastructure.'
MODULE_EXPLAIN[C]=$'Multi-Account Networking\nWhat: VPC, private subnets, and AgentCore VPC endpoints (org-restricted policy when ORG_ID is set).\nWhy: enterprise network isolation — agent traffic stays on the AWS backbone.'
MODULE_EXPLAIN[E]=$'Security Automation\nWhat: KMS customer-managed key encryption + CloudTrail audit logging (plus opt-in guardrail controls).\nWhy: enterprise security baselines — customer-managed keys and a full audit trail of AgentCore API calls.'

# ── Guided workshop verification (workshop action) ──
# Each entry is a shell snippet run via `bash -c` from the project root after
# the module deploys (run_verify exports PROJECT_NAME/ENVIRONMENT/PREFIX/AWS_REGION).
# 5/6/7/A reuse the live-proven test scripts; the rest are cheap SSM/CFN presence checks.
declare -A MODULE_VERIFY
# Single quotes are deliberate throughout this block: the snippets are expanded
# later, inside `bash -c` at verify time (AWS_REGION isn't final until
# check_credentials runs). The group scopes one directive over all entries.
# shellcheck disable=SC2016
{
MODULE_VERIFY[3]='aws ssm get-parameter --name "/$PROJECT_NAME/$ENVIRONMENT/auth/issuer-url" --region "$AWS_REGION" --query Parameter.Value --output text'
MODULE_VERIFY[4]='aws ssm get-parameter --name "/$PROJECT_NAME/$ENVIRONMENT/identity/gateway-credential-provider-name" --region "$AWS_REGION" --query Parameter.Value --output text'
MODULE_VERIFY[5]='.venv/bin/python scripts/test_gateway.py'
MODULE_VERIFY[6]='.venv/bin/python scripts/invoke.py "Reply with exactly: WORKSHOP OK"'
MODULE_VERIFY[7]='.venv/bin/python scripts/test_gateway.py'
MODULE_VERIFY[8]='.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"'
MODULE_VERIFY[9]='.venv/bin/python scripts/check_observability.py'
MODULE_VERIFY[A]='.venv/bin/python scripts/test_memory.py'
MODULE_VERIFY[C]='aws cloudformation describe-stacks --stack-name "$PREFIX-networking" --region "$AWS_REGION" --query "Stacks[0].StackStatus" --output text | grep -q COMPLETE && .venv/bin/python scripts/check_network.py'
MODULE_VERIFY[E]='aws cloudformation describe-stacks --stack-name "$PREFIX-security" --region "$AWS_REGION" --query "Stacks[0].StackStatus" --output text | grep -q COMPLETE'
}

# ── Team Workstream Assignments (Requirement 20) ──
declare -A TEAM_MAP
TEAM_MAP[platform]="${PREFIX}-networking ${PREFIX}-auth ${PREFIX}-identity ${PREFIX}-gateway ${PREFIX}-observability"
TEAM_MAP[agent]="${PREFIX}-runtime-orchestrator ${PREFIX}-runtime-code-agent ${PREFIX}-runtime-research-agent ${PREFIX}-memory"
TEAM_MAP[security]="${PREFIX}-security ${PREFIX}-observability"

# ── Profile → Feature Flags (Requirement 13) ──
declare -A PROFILE_FLAGS
PROFILE_FLAGS[greenfield]="enable_networking=false enable_security=false enable_a2a=false"
PROFILE_FLAGS[migration]="enable_networking=false enable_security=false enable_a2a=false"
PROFILE_FLAGS[multi-agent]="enable_networking=false enable_security=false enable_a2a=true"
PROFILE_FLAGS[platform-team]="enable_networking=true enable_security=true enable_a2a=true"
PROFILE_FLAGS[security-focused]="enable_networking=true enable_security=true enable_a2a=false enable_resource_policies=true enable_egress_filter=true enable_cedar=true enable_traceability=true"

# ── Profile → Guided Workshop Module Sequence (workshop action) ──
# Which modules each customer profile walks through, in order (README facilitator guide).
declare -A PROFILE_MODULES
PROFILE_MODULES[greenfield]="3 4 5 6 9"
PROFILE_MODULES[migration]="3 4 6 7 9"
PROFILE_MODULES[multi-agent]="3 4 5 6 7 8 9"
# Memory (A) precedes Agent Deployment (6): the orchestrator runtime depends on
# the memory stack, so CDK would create it implicitly at module 6 and module A
# would then report "no changes" — deploying it in its own module keeps the
# narrative honest.
PROFILE_MODULES[platform-team]="3 4 5 A 6 7 8 9 C E"
PROFILE_MODULES[security-focused]="3 4 5 6 9 E"

# ═══════════════════════════════════════════════════════════════
# Prerequisite Checks (Requirement 18)
# ═══════════════════════════════════════════════════════════════
check_prereqs() {
    log_header "Prerequisite Checks"
    local missing=0

    # Note: docker is NOT required — container images are built remotely in AWS CodeBuild.
    for cmd in node npm python3.13 aws; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            case "$cmd" in
                node)       ver=$(node --version) ;;
                npm)        ver=$(npm --version) ;;
                python3.13) ver=$(python3.13 --version 2>&1) ;;
                aws)        ver=$(aws --version 2>&1 | head -1) ;;
            esac
            log_info "✓ $cmd: $ver"
        else
            log_error "✗ $cmd: NOT FOUND"
            missing=1
        fi
    done

    # Optional: report docker if present, but don't fail without it.
    if command -v docker &>/dev/null; then
        log_info "✓ docker: $(docker --version 2>&1) (optional — used only for local container testing)"
    else
        log_info "○ docker: not installed (optional — image builds run in AWS CodeBuild)"
    fi

    # Check CDK CLI. --no-install is what keeps this from hanging: a bare
    # `npx cdk` prints "Need to install the following packages ... Ok to
    # proceed?" when aws-cdk is not in the npx cache, and with the output
    # suppressed and no TTY (CI, a background deploy, the guided wizard) that
    # wait never ends — the probe never returns, so the install below never ran.
    # --no-install fails fast instead. npx still prefers a cdk already on PATH.
    #
    # The other `npx cdk` calls in this script stay safe because of this: past
    # here the CLI is resolvable, or we exited.
    local cdk_version
    if cdk_version=$(npx --no-install cdk --version 2>/dev/null); then
        log_info "✓ cdk: $cdk_version"
    else
        log_warn "CDK CLI not found, installing..."
        if npm install -g aws-cdk; then
            log_info "✓ cdk: installed"
        else
            log_error "✗ cdk: 'npm install -g aws-cdk' failed — install it manually and retry"
            missing=1
        fi
    fi

    if [ "$missing" -eq 1 ]; then
        log_error "Install missing prerequisites and retry."
        exit 1
    fi
}

# ═══════════════════════════════════════════════════════════════
# AWS Credential Validation (Requirement 18.4)
# ═══════════════════════════════════════════════════════════════
check_credentials() {
    log_header "AWS Credentials"
    if ! aws sts get-caller-identity &>/dev/null; then
        log_error "AWS credentials invalid or expired."
        log_error "Run 'aws sso login' or configure credentials, then retry."
        exit 1
    fi
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    AWS_REGION=${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-east-1")}
    local identity
    identity=$(aws sts get-caller-identity --query Arn --output text)
    log_info "Account:  $ACCOUNT_ID"
    log_info "Region:   $AWS_REGION"
    log_info "Identity: $identity"
    export CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID"
    export CDK_DEFAULT_REGION="$AWS_REGION"
}

# ═══════════════════════════════════════════════════════════════
# ORG_ID Validation (fail fast before any CDK command)
# ═══════════════════════════════════════════════════════════════
check_org_id() {
    # enable_resource_policies=true makes the memory stack raise a ValueError
    # mid-deploy when org_id is empty — catch that here instead.
    if [ "${ENABLE_RESOURCE_POLICIES:-}" = "true" ] && [ -z "${ORG_ID:-}" ]; then
        if [ "${NON_INTERACTIVE:-0}" != "1" ]; then
            echo ""
            log_warn "Resource policies are enabled and require your AWS Organizations ID."
            read -rp "  AWS Organizations ID (o-xxxx): " ORG_ID
        fi
        if [ -z "${ORG_ID:-}" ]; then
            log_error "enable_resource_policies=true requires an AWS Organizations ID."
            log_error "The memory stack resource policy uses aws:PrincipalOrgID and cannot render without it."
            log_error "Fix: export ORG_ID=o-xxxx and re-run."
            log_error "(Find it with: aws organizations describe-organization --query Organization.Id --output text)"
            exit 1
        fi
        export ORG_ID
        save_config
    fi

    # The networking stack silently skips the AgentCore VPC endpoint policy
    # without org_id — warn so the user knows the endpoint is unrestricted.
    if [ "${ENABLE_NETWORKING:-}" = "true" ] && [ -z "${ORG_ID:-}" ]; then
        log_warn "enable_networking=true without ORG_ID: the AgentCore VPC endpoint"
        log_warn "policy will be skipped (endpoint deploys without an org-restricted policy)."
        log_warn "To apply it: export ORG_ID=o-xxxx and re-deploy."
    fi
}

# ═══════════════════════════════════════════════════════════════
# Interactive Configuration (Requirement 3)
# ═══════════════════════════════════════════════════════════════
prompt_region() {
    if [ "${NON_INTERACTIVE:-0}" = "1" ]; then return; fi
    echo ""
    echo "Available regions:"
    echo "  1) us-east-1 (N. Virginia)"
    echo "  2) us-west-2 (Oregon)"
    echo "  3) eu-west-1 (Ireland)"
    echo "  4) eu-central-1 (Frankfurt)"
    echo "  5) ap-northeast-1 (Tokyo)"
    echo "  6) ap-southeast-1 (Singapore)"
    read -rp "Select region [${AWS_REGION:+saved: }${AWS_REGION:-1}]: " choice
    case "${choice:-keep}" in
        1) AWS_REGION="us-east-1" ;; 2) AWS_REGION="us-west-2" ;;
        3) AWS_REGION="eu-west-1" ;; 4) AWS_REGION="eu-central-1" ;;
        5) AWS_REGION="ap-northeast-1" ;; 6) AWS_REGION="ap-southeast-1" ;;
        keep) AWS_REGION="${AWS_REGION:-us-east-1}" ;;
        *) AWS_REGION="us-east-1" ;;
    esac
    export CDK_DEFAULT_REGION="$AWS_REGION"
    log_info "Region set to: $AWS_REGION"
}

prompt_agent_pattern() {
    if [ "${NON_INTERACTIVE:-0}" = "1" ]; then return; fi
    # shellcheck disable=SC2206  # deliberate word split: AGENT_PATTERNS is a fixed literal list
    local choice i=1 pats=($AGENT_PATTERNS)
    echo ""
    echo "Agent framework pattern (the container the runtime builds):"
    for p in "${pats[@]}"; do echo "  $i) $p"; i=$((i + 1)); done
    read -rp "Select pattern [keep: ${AGENT_PATTERN}]: " choice
    # Accept the menu number or the pattern name; anything else keeps the current value.
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#pats[@]}" ]; then
        AGENT_PATTERN="${pats[$((choice - 1))]}"
    elif [ -n "$choice" ] && [[ " $AGENT_PATTERNS " == *" $choice "* ]]; then
        AGENT_PATTERN="$choice"
    elif [ -n "$choice" ]; then
        log_warn "Unknown pattern '$choice' — keeping ${AGENT_PATTERN}"
    fi
    log_info "Agent pattern set to: $AGENT_PATTERN"
}

prompt_idp() {
    if [ "${NON_INTERACTIVE:-0}" = "1" ]; then return; fi
    if [ -n "${IDP_TYPE:-}" ]; then
        echo ""
        read -rp "IdP: ${IDP_TYPE} (saved) — keep? [Y/n]: " keep
        if [[ ! "$keep" =~ ^[Nn] ]]; then
            # The client secret is never persisted: reuse the one already in
            # Secrets Manager if present, otherwise re-prompt for it.
            if [ "$IDP_TYPE" != "cognito" ] && [ -z "${IDP_CLIENT_SECRET:-}" ]; then
                # Bring-your-own secret: if IDP_CLIENT_SECRET_NAME is configured
                # (platform.yaml or env), look for THAT secret and keep its name.
                # Probing only the prefixed name would ignore the secret the
                # operator already owns, prompt for the value anyway, and create
                # a duplicate under our name.
                local secret_name="${IDP_CLIENT_SECRET_NAME:-${PREFIX}-idp-client-secret}"
                if aws secretsmanager describe-secret \
                    --secret-id "$secret_name" \
                    --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
                    IDP_CLIENT_SECRET_NAME="$secret_name"
                    log_info "✓ IdP client secret: reusing existing Secrets Manager secret ($secret_name)"
                else
                    read -rsp "  ${IDP_TYPE} Client Secret: " IDP_CLIENT_SECRET; echo ""
                fi
            fi
            log_info "IdP set to: ${IDP_TYPE}"
            return
        fi
        unset IDP_TYPE IDP_TENANT_ID IDP_CLIENT_ID IDP_ISSUER_URL
    fi
    echo ""
    echo "Identity Provider:"
    echo "  1) Amazon Cognito (default)"
    echo "  2) Microsoft Entra ID"
    echo "  3) Okta"
    echo "  4) Ping Identity"
    read -rp "Select IdP [1]: " choice
    case "${choice:-1}" in
        1) IDP_TYPE="cognito" ;;
        2) IDP_TYPE="entra_id"
           read -rp "  Entra ID Tenant ID: " IDP_TENANT_ID
           read -rp "  Entra ID Client ID: " IDP_CLIENT_ID
           read -rsp "  Entra ID Client Secret: " IDP_CLIENT_SECRET; echo ""
           ;;
        3) IDP_TYPE="okta"
           read -rp "  Okta Issuer URL: " IDP_ISSUER_URL
           read -rp "  Okta Client ID: " IDP_CLIENT_ID
           read -rsp "  Okta Client Secret: " IDP_CLIENT_SECRET; echo ""
           ;;
        4) IDP_TYPE="ping"
           read -rp "  Ping Issuer URL: " IDP_ISSUER_URL
           read -rp "  Ping Client ID: " IDP_CLIENT_ID
           read -rsp "  Ping Client Secret: " IDP_CLIENT_SECRET; echo ""
           ;;
    esac
    log_info "IdP set to: ${IDP_TYPE:-cognito}"
}

prompt_api_keys() {
    if [ "${NON_INTERACTIVE:-0}" = "1" ]; then return; fi
    log_header "API Keys (Optional)"

    for key_name in tavily google-search google-maps; do
        local secret_name="${PREFIX}-${key_name}-api-key"
        if aws secretsmanager describe-secret --secret-id "$secret_name" &>/dev/null 2>&1; then
            log_info "✓ ${key_name}: already configured"
        else
            read -rp "  ${key_name} API key (Enter to skip): " api_key
            if [ -n "$api_key" ]; then
                aws secretsmanager create-secret --name "$secret_name" \
                    --secret-string "$api_key" --region "$AWS_REGION" &>/dev/null
                log_info "✓ ${key_name}: stored in Secrets Manager"
            else
                log_info "○ ${key_name}: skipped"
            fi
        fi
    done
}

# ═══════════════════════════════════════════════════════════════
# IdP Client Secret → Secrets Manager
# ═══════════════════════════════════════════════════════════════
# The enterprise IdP client secret must never travel as a plaintext CDK
# context arg (visible in `ps` output and rendered into the synthesized
# template). Instead we upsert it into Secrets Manager and pass only the
# secret NAME; the auth stack resolves the value at deploy time via a
# {{resolve:secretsmanager:...}} CloudFormation dynamic reference.
#
# upsert_oauth_secret VALUE_VAR NAME_VAR DEFAULT_NAME LABEL
#   VALUE_VAR    name of the variable holding the plaintext (unset afterwards)
#   NAME_VAR     name of the variable holding/receiving the secret's name
#   DEFAULT_NAME secret name used when the operator did not configure one
#   LABEL        human label for log messages
upsert_oauth_secret() {
    local value_var="$1" name_var="$2" default_name="$3" label="$4"
    local value="${!value_var:-}"
    if [ -z "$value" ]; then return 0; fi
    # Strip surrounding whitespace. A secret pasted from a console, or piped in
    # from a CLI (`az ... -o tsv`), arrives with a trailing newline; it is
    # stored and forwarded verbatim, and the provider's token endpoint rejects
    # the exchange with invalid_client, naming nothing about whitespace. Cost
    # an hour to find live — see docs/ENTERPRISE_IDP.md.
    value="$(printf '%s' "$value" | tr -d '\n\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -z "$value" ]; then
        log_error "$value_var is only whitespace — nothing to store."
        exit 1
    fi
    # Write to the operator's secret when they named one (platform.yaml / env),
    # otherwise to our own. Rotating into a bring-your-own secret must not
    # silently fork a second copy under the prefixed name.
    local secret_name="${!name_var:-$default_name}"
    # Exported, not just assigned: app.py must see the name even on CDK calls
    # that carry no context args (the bootstrap probe synthesizes the app too).
    printf -v "$name_var" '%s' "$secret_name"
    export "${name_var?}"
    if aws secretsmanager describe-secret --secret-id "$secret_name" \
        --region "$AWS_REGION" &>/dev/null; then
        # Secret already exists — update the value (client secrets rotate).
        if ! aws secretsmanager put-secret-value --secret-id "$secret_name" \
            --secret-string "$value" --region "$AWS_REGION" &>/dev/null; then
            log_error "Failed to update Secrets Manager secret '$secret_name'."
            log_error "Check IAM permissions for secretsmanager:PutSecretValue and retry."
            exit 1
        fi
        log_info "✓ $label secret: updated in Secrets Manager ($secret_name)"
    else
        if ! aws secretsmanager create-secret --name "$secret_name" \
            --secret-string "$value" --region "$AWS_REGION" &>/dev/null; then
            log_error "Failed to create Secrets Manager secret '$secret_name'."
            log_error "Check IAM permissions for secretsmanager:CreateSecret and retry."
            exit 1
        fi
        log_info "✓ $label secret: stored in Secrets Manager ($secret_name)"
    fi
    # Plaintext is no longer needed — only the secret name is passed to CDK.
    unset "$value_var"
}

upsert_idp_secret() {
    upsert_oauth_secret IDP_CLIENT_SECRET IDP_CLIENT_SECRET_NAME \
        "${PREFIX}-idp-client-secret" "IdP client"
}

# 3LO providers (module 4): GOOGLE/GITHUB/NOTION_CLIENT_SECRET in the
# environment is moved to Secrets Manager the same way — app.py refuses the
# plaintext form outright.
upsert_3lo_secrets() {
    upsert_oauth_secret GOOGLE_CLIENT_SECRET GOOGLE_CLIENT_SECRET_NAME \
        "${PREFIX}-google-oauth-secret" "Google OAuth"
    upsert_oauth_secret GITHUB_CLIENT_SECRET GITHUB_CLIENT_SECRET_NAME \
        "${PREFIX}-github-oauth-secret" "GitHub OAuth"
    upsert_oauth_secret NOTION_CLIENT_SECRET NOTION_CLIENT_SECRET_NAME \
        "${PREFIX}-notion-oauth-secret" "Notion OAuth"
}

# ═══════════════════════════════════════════════════════════════
# Python Virtual Environment
# ═══════════════════════════════════════════════════════════════
setup_venv() {
    if [ ! -d "$PROJECT_DIR/.venv" ]; then
        log_step "Creating Python virtual environment..."
        python3.13 -m venv "$PROJECT_DIR/.venv"
    fi
    # shellcheck source=/dev/null  # generated by venv above; absent at lint time
    source "$PROJECT_DIR/.venv/bin/activate"
    pip install -q -r "$PROJECT_DIR/requirements.txt" 2>/dev/null
}

# ═══════════════════════════════════════════════════════════════
# CDK Context Builder
# ═══════════════════════════════════════════════════════════════
# Populates the global CONTEXT_ARGS array. Using an array (expanded as
# "${CONTEXT_ARGS[@]}") keeps values containing spaces intact — a single
# string expanded unquoted would word-split them.
CONTEXT_ARGS=()
build_context_args() {
    CONTEXT_ARGS=()
    CONTEXT_ARGS+=(-c "project=${PROJECT_NAME}")
    CONTEXT_ARGS+=(-c "environment=${ENVIRONMENT}")
    CONTEXT_ARGS+=(-c "region=${AWS_REGION:-us-east-1}")
    CONTEXT_ARGS+=(-c "idp_type=${IDP_TYPE:-cognito}")

    # IdP config — the client secret itself is never passed; only the name of
    # the Secrets Manager secret set by upsert_idp_secret (see above).
    [ -n "${IDP_TENANT_ID:-}" ]          && CONTEXT_ARGS+=(-c "idp_tenant_id=${IDP_TENANT_ID}")
    [ -n "${IDP_CLIENT_ID:-}" ]          && CONTEXT_ARGS+=(-c "idp_client_id=${IDP_CLIENT_ID}")
    [ -n "${IDP_CLIENT_SECRET_NAME:-}" ] && CONTEXT_ARGS+=(-c "idp_client_secret_name=${IDP_CLIENT_SECRET_NAME}")
    [ -n "${IDP_ISSUER_URL:-}" ]         && CONTEXT_ARGS+=(-c "idp_issuer_url=${IDP_ISSUER_URL}")

    # 3LO provider config — same rule: secret NAMES only (upsert_3lo_secrets).
    [ -n "${GOOGLE_CLIENT_ID:-}" ]          && CONTEXT_ARGS+=(-c "google_client_id=${GOOGLE_CLIENT_ID}")
    [ -n "${GOOGLE_CLIENT_SECRET_NAME:-}" ] && CONTEXT_ARGS+=(-c "google_client_secret_name=${GOOGLE_CLIENT_SECRET_NAME}")
    [ -n "${GITHUB_CLIENT_ID:-}" ]          && CONTEXT_ARGS+=(-c "github_client_id=${GITHUB_CLIENT_ID}")
    [ -n "${GITHUB_CLIENT_SECRET_NAME:-}" ] && CONTEXT_ARGS+=(-c "github_client_secret_name=${GITHUB_CLIENT_SECRET_NAME}")
    [ -n "${NOTION_CLIENT_ID:-}" ]          && CONTEXT_ARGS+=(-c "notion_client_id=${NOTION_CLIENT_ID}")
    [ -n "${NOTION_CLIENT_SECRET_NAME:-}" ] && CONTEXT_ARGS+=(-c "notion_client_secret_name=${NOTION_CLIENT_SECRET_NAME}")

    # Feature flags from profile
    [ -n "${ENABLE_NETWORKING:-}" ] && CONTEXT_ARGS+=(-c "enable_networking=${ENABLE_NETWORKING}")
    [ -n "${ENABLE_SECURITY:-}" ]   && CONTEXT_ARGS+=(-c "enable_security=${ENABLE_SECURITY}")
    [ -n "${ENABLE_A2A:-}" ]        && CONTEXT_ARGS+=(-c "enable_a2a=${ENABLE_A2A}")

    # Security control feature flags (control-library / scope-split model)
    [ -n "${ENABLE_RESOURCE_POLICIES:-}" ] && CONTEXT_ARGS+=(-c "enable_resource_policies=${ENABLE_RESOURCE_POLICIES}")
    [ -n "${ENABLE_EGRESS_FILTER:-}" ]     && CONTEXT_ARGS+=(-c "enable_egress_filter=${ENABLE_EGRESS_FILTER}")
    [ -n "${ENABLE_CEDAR:-}" ]             && CONTEXT_ARGS+=(-c "enable_cedar=${ENABLE_CEDAR}")
    [ -n "${CEDAR_MODE:-}" ]               && CONTEXT_ARGS+=(-c "cedar_mode=${CEDAR_MODE}")
    [ -n "${ENABLE_TRACEABILITY:-}" ]      && CONTEXT_ARGS+=(-c "enable_traceability=${ENABLE_TRACEABILITY}")
    [ -n "${ORG_ID:-}" ]                   && CONTEXT_ARGS+=(-c "org_id=${ORG_ID}")

    # Optional Bedrock model ID override (agents fall back to their in-code default)
    [ -n "${MODEL_ID:-}" ]                 && CONTEXT_ARGS+=(-c "model_id=${MODEL_ID}")

    # Agent framework pattern (validated above; always set)
    CONTEXT_ARGS+=(-c "agent_pattern=${AGENT_PATTERN}")

    return 0
}

# ═══════════════════════════════════════════════════════════════
# Full-footprint confirmation (deploy --all / destroy --all)
# ═══════════════════════════════════════════════════════════════
# A deploy or destroy with no target touches EVERYTHING the app synthesizes,
# so it shows the account, the config source, and the exact stack list first.
# --yes skips the prompt; NON_INTERACTIVE=1 implies --yes (CI compatibility).
confirm_footprint() {
    # $1 = verb ("deploy" | "destroy")
    [ "$YES" = "1" ] && return 0
    [ "${NON_INTERACTIVE:-0}" = "1" ] && return 0
    echo ""
    log_header "Plan — $1 the FULL footprint"
    log_info "Account: ${ACCOUNT_ID:-unknown}   Region: ${AWS_REGION:-us-east-1}   Prefix: ${PREFIX}"
    local src="built-in defaults"
    [ -f "$CONFIG_FILE" ] && src="workshop.env"
    [ -f "$PLATFORM_CONFIG" ] && src="platform.yaml"
    log_info "Config source: $src"
    log_info "Stacks:"
    JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
        npx cdk ls "${CONTEXT_ARGS[@]}" 2>/dev/null | sed 's/^/    /' \
        || log_warn "(could not synthesize the stack list)"
    local ans
    read -rp "Proceed to $1 ALL of the above? [y/N]: " ans
    case "$ans" in
        [Yy]*) : ;;
        *) log_error "Aborted before any change. Re-run with --yes to skip this prompt."; exit 1 ;;
    esac
}

# ═══════════════════════════════════════════════════════════════
# Stack Deploy Loop (shared by the deploy and workshop actions)
# ═══════════════════════════════════════════════════════════════
deploy_stacks() {
    local stack
    for stack in "$@"; do
        log_step "Deploying: $stack"
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk deploy "$stack" --require-approval never "${CONTEXT_ARGS[@]}" 2>&1 || {
            log_error "Failed to deploy $stack"
            log_error "Check CloudFormation console for details."
            exit 1
        }
    done
}

# The destroy counterpart. `cdk destroy STACK` without --exclusively walks the
# dependency graph and deletes every stack that DEPENDS ON the target too — the
# mirror image of `cdk deploy` pulling dependencies in. Tearing down one module
# would silently remove the shared platform from under the other teams (the
# blast radius grew when the runtimes gained a dependency on networking), so a
# targeted destroy is always --exclusively. CloudFormation then refuses with
# "Export ... cannot be deleted as it is in use by <stack>", naming the consumer.
# The cascade stays for `destroy --all`, where it is what was asked for.
destroy_stacks() {
    local stack rc=0
    if [ "$#" -eq 0 ]; then
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk destroy --all --force "${CONTEXT_ARGS[@]}" 2>&1 || rc=1
        return "$rc"
    fi
    for stack in "$@"; do
        log_step "Destroying: $stack (this stack only)"
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk destroy "$stack" --exclusively --force "${CONTEXT_ARGS[@]}" 2>&1 || {
            rc=1
            log_error "Failed to destroy $stack"
            log_error "If the error above names an export in use, another stack depends on this one."
            log_error "Destroy the dependents first, or run 'destroy --all' for the whole environment."
        }
    done
    return "$rc"
}

cdk_bootstrap() {
    log_header "CDK Bootstrap"
    local bootstrap_log
    bootstrap_log=$(mktemp)
    if JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
        npx cdk bootstrap "aws://$ACCOUNT_ID/$AWS_REGION" >"$bootstrap_log" 2>&1; then
        # "Already bootstrapped" also exits 0 — both are success.
        log_info "Bootstrap OK for aws://$ACCOUNT_ID/$AWS_REGION"
        rm -f "$bootstrap_log"
    else
        log_error "CDK bootstrap failed for aws://$ACCOUNT_ID/$AWS_REGION. Output:"
        cat "$bootstrap_log"
        rm -f "$bootstrap_log"
        log_error "Common causes: wrong AWS account/profile, or missing IAM permissions"
        log_error "to create the CDK bootstrap stack (CDKToolkit). Fix and retry."
        exit 1
    fi
}

# ═══════════════════════════════════════════════════════════════
# Guided Workshop Helpers (workshop action)
# ═══════════════════════════════════════════════════════════════
pause() {
    [ "${NON_INTERACTIVE:-0}" = "1" ] && return 0
    echo ""
    echo -e "${BOLD}Press ENTER to continue...${NC}"
    read -r
}

run_verify() {
    local module="$1" cont
    log_step "Verifying module $module: ${MODULE_VERIFY[$module]}"
    if PROJECT_NAME="$PROJECT_NAME" ENVIRONMENT="$ENVIRONMENT" PREFIX="$PREFIX" \
        AWS_REGION="${AWS_REGION:-us-east-1}" bash -c "${MODULE_VERIFY[$module]}"; then
        log_info "✓ Module $module verified"
    else
        log_error "✗ Module $module verification failed"
        if [ "${NON_INTERACTIVE:-0}" = "1" ]; then
            log_error "Aborting (NON_INTERACTIVE=1 cannot prompt)."
            exit 1
        fi
        read -rp "Continue anyway? [y/N]: " cont
        [[ "$cont" =~ ^[Yy] ]] || { log_error "Aborting workshop."; exit 1; }
    fi
}

# ═══════════════════════════════════════════════════════════════
# Deploy Summary (Requirement 2.5)
# ═══════════════════════════════════════════════════════════════
print_summary() {
    log_header "Deployment Summary"
    for stack in $(aws cloudformation list-stacks \
        --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
        --query "StackSummaries[?starts_with(StackName,'${PREFIX}')].StackName" \
        --output text --region "$AWS_REGION" 2>/dev/null); do
        echo -e "\n${BOLD}${BLUE}$stack${NC}:"
        aws cloudformation describe-stacks --stack-name "$stack" --region "$AWS_REGION" \
            --query "Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}" \
            --output table 2>/dev/null || true
    done
}

# ═══════════════════════════════════════════════════════════════
# Export Utility (Requirement 15)
# ═══════════════════════════════════════════════════════════════
export_artifacts() {
    log_header "Exporting Artifacts"
    local stamp export_file
    stamp="$(date +%Y%m%d-%H%M%S)"
    export_file="${PROJECT_DIR}/workshop-outputs-${stamp}.json"

    # Collect all SSM parameters
    local params
    params=$(aws ssm get-parameters-by-path \
        --path "/${PROJECT_NAME}/${ENVIRONMENT}" \
        --recursive --region "$AWS_REGION" \
        --query "Parameters[*].{Name:Name,Value:Value}" \
        --output json 2>/dev/null || echo "[]")

    # Collect all stack outputs
    local outputs="[]"
    for stack in $(aws cloudformation list-stacks \
        --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
        --query "StackSummaries[?starts_with(StackName,'${PREFIX}')].StackName" \
        --output text --region "$AWS_REGION" 2>/dev/null); do
        local stack_outputs
        stack_outputs=$(aws cloudformation describe-stacks --stack-name "$stack" --region "$AWS_REGION" \
            --query "Stacks[0].Outputs" --output json 2>/dev/null || echo "[]")
        outputs=$(echo "$outputs $stack_outputs" | python3.13 -c "
import json, sys
parts = sys.stdin.read().split()
result = []
for p in parts:
    try: result.extend(json.loads(p))
    except: pass
print(json.dumps(result))
")
    done

    python3.13 -c "
import json
data = {
    'project': '${PROJECT_NAME}',
    'environment': '${ENVIRONMENT}',
    'region': '${AWS_REGION}',
    'account_id': '${ACCOUNT_ID}',
    'ssm_parameters': json.loads('''${params}'''),
    'stack_outputs': json.loads('''${outputs}'''),
}
with open('${export_file}', 'w') as f:
    json.dump(data, f, indent=2)
"
    log_info "Exported to: ${export_file}"
}

# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
ACTION="${1:-deploy}"
shift || true

# ── 'config' action: show or reset saved answers (no AWS access needed) ──
if [ "$ACTION" = "config" ]; then
    if [ "${1:-}" = "--reset" ]; then
        rm -f "$CONFIG_FILE"
        log_info "Removed workshop.env"
        exit 0
    fi
    if [ -f "$PLATFORM_CONFIG" ]; then
        echo "# platform.yaml → effective values (env vars override):"
        py="$PROJECT_DIR/.venv/bin/python"; [ -x "$py" ] || py="python3"
        "$py" -m infra_utils.platform_config --export "$PLATFORM_CONFIG" || exit 1
        echo ""
    fi
    if [ -f "$CONFIG_FILE" ]; then
        echo "# workshop.env (wizard answers; platform.yaml overrides):"
        cat "$CONFIG_FILE"
    elif [ ! -f "$PLATFORM_CONFIG" ]; then
        echo "none"
    fi
    exit 0
fi

# Parse flags
STACK_FILTER=""
PROFILE=""
TEAM=""
MODULE=""
FROM_MODULE=""
DRY_RUN=0
YES=0
require_flag_value() {
    # $1 = flag name, $2 = number of remaining args after the flag
    if [ "$2" -lt 2 ]; then
        log_error "$1 requires a value (e.g. $1 <value>)."
        exit 1
    fi
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stack)   require_flag_value "--stack" "$#";   STACK_FILTER="$2"; shift 2 ;;
        --profile) require_flag_value "--profile" "$#"; PROFILE="$2"; shift 2 ;;
        --team)    require_flag_value "--team" "$#";    TEAM="$2"; shift 2 ;;
        --module)  require_flag_value "--module" "$#";  MODULE="$2"; shift 2 ;;
        --from)    require_flag_value "--from" "$#";    FROM_MODULE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes)     YES=1; shift ;;
        *)
            # Fail closed: `--stakc identity` or `--stack=identity` used to be
            # silently discarded here, leaving no target and escalating to
            # `cdk deploy --all`. A typo must never deploy more than asked.
            log_error "Unknown option or argument: '$1'"
            log_error "Valid options: --stack --profile --team --module --from --dry-run --yes"
            log_error "(values are space-separated: --stack NAME, not --stack=NAME)"
            exit 1
            ;;
    esac
done

# Workshop action: default the profile.
[ "$ACTION" = "workshop" ] && PROFILE="${PROFILE:-greenfield}"

# ── Fail closed on VALUES, for every action ──
# A misspelled --profile used to be validated only for the workshop action:
# on plain deploy it skipped its feature flags without a word and fell
# through to `cdk deploy --all` — a typo produced a LARGER deployment.
if [ -n "$PROFILE" ] && [ -z "${PROFILE_FLAGS[$PROFILE]:-}" ]; then
    log_error "Unknown profile: '$PROFILE'. Valid profiles: ${!PROFILE_FLAGS[*]}"
    exit 1
fi
if [ -n "$TEAM" ] && [ -z "${TEAM_MAP[$TEAM]:-}" ]; then
    log_error "Unknown team: '$TEAM'. Valid teams: ${!TEAM_MAP[*]}"
    exit 1
fi
# --stack takes full stack names; a short name like 'identity' would only
# fail deep inside cdk ("No stacks match") after bootstrap has already run.
for _s in ${STACK_FILTER:-}; do
    case "$_s" in
        "$PREFIX"-*) : ;;
        *)
            log_error "Unknown stack: '$_s' — stacks are full names like ${PREFIX}-identity."
            log_error "List them with: $0 ls"
            exit 1
            ;;
    esac
done

# Apply profile flags
if [ -n "$PROFILE" ] && [ -n "${PROFILE_FLAGS[$PROFILE]:-}" ]; then
    log_info "Profile: $PROFILE"
    for flag in ${PROFILE_FLAGS[$PROFILE]}; do
        key="${flag%%=*}"
        val="${flag##*=}"
        export "$(echo "$key" | tr '[:lower:]' '[:upper:]')=$val"
    done
fi

# Fail fast on missing ORG_ID before any CDK command runs
check_org_id

# Resolve stack targets
CDK_STACKS=""
if [ -n "$STACK_FILTER" ]; then
    CDK_STACKS="$STACK_FILTER"
elif [ "$MODULE" = "D" ]; then
    log_info "Module D (CI/CD Pipeline) is a guided module — see .gitlab-ci.yml as the reference implementation; no stacks to deploy."
    exit 0
elif [ -n "$MODULE" ] && [ -n "${MODULE_MAP[$MODULE]:-}" ]; then
    CDK_STACKS="${MODULE_MAP[$MODULE]}"
    log_info "Workshop Module $MODULE → Stacks: $CDK_STACKS"
elif [ -n "$MODULE" ]; then
    log_error "Unknown module: '$MODULE'. Valid modules: $VALID_MODULES"
    exit 1
elif [ -n "$TEAM" ] && [ -n "${TEAM_MAP[$TEAM]:-}" ]; then
    CDK_STACKS="${TEAM_MAP[$TEAM]}"
    log_info "Team $TEAM → Stacks: $CDK_STACKS"
fi

# A workshop dry run makes zero AWS calls — skip everything that needs them.
if [ "$ACTION" = "workshop" ] && [ "$DRY_RUN" = "1" ]; then
    log_info "Dry run: skipping prerequisite and credential checks (no AWS calls)"
else
    check_prereqs
    check_credentials
    setup_venv
fi

cd "$PROJECT_DIR"

# If IDP_CLIENT_SECRET or a 3LO *_CLIENT_SECRET came from the environment, move
# it into Secrets Manager before any context args are built (plaintext never
# reaches the CDK CLI).
if [ "$DRY_RUN" != "1" ]; then
    upsert_idp_secret
    upsert_3lo_secrets
fi

# Build CDK context args (populates the CONTEXT_ARGS array)
build_context_args

case "$ACTION" in
    deploy)
        if [ "${NON_INTERACTIVE:-0}" != "1" ]; then
            prompt_region
            prompt_agent_pattern
            prompt_idp
            prompt_api_keys
            upsert_idp_secret   # Store any newly prompted IdP secret; sets IDP_CLIENT_SECRET_NAME
            build_context_args  # Rebuild with new values
            save_config         # Persist answers for the next run (secrets excluded)
        fi

        cdk_bootstrap

        log_header "Deploying"
        if [ -n "$CDK_STACKS" ]; then
            # shellcheck disable=SC2086  # stack list is space-separated by design
            deploy_stacks $CDK_STACKS
        else
            confirm_footprint deploy
            log_step "Deploying all stacks..."
            JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
                npx cdk deploy --all --require-approval never "${CONTEXT_ARGS[@]}" 2>&1 || {
                log_error "Deployment failed. Check CloudFormation console for details."
                exit 1
            }
        fi

        print_summary
        ;;

    workshop)
        SEQUENCE="${PROFILE_MODULES[$PROFILE]}"
        if [ -n "$FROM_MODULE" ]; then
            case " $SEQUENCE " in
                *" $FROM_MODULE "*) : ;;
                *) log_error "--from $FROM_MODULE is not in the '$PROFILE' sequence ($SEQUENCE)"; exit 1 ;;
            esac
        fi

        log_header "Guided Workshop — profile: $PROFILE"
        log_info "Module sequence: $SEQUENCE"
        log_info "Agent pattern: $AGENT_PATTERN"
        [ "$DRY_RUN" = "1" ] && log_info "DRY RUN — nothing will be deployed"

        if [ "$DRY_RUN" != "1" ] && [ "${NON_INTERACTIVE:-0}" != "1" ]; then
            prompt_region
            prompt_agent_pattern
            prompt_idp
            prompt_api_keys
            upsert_idp_secret   # Store any newly prompted IdP secret; sets IDP_CLIENT_SECRET_NAME
            build_context_args  # Rebuild with new values
            save_config         # Persist answers for the next run (secrets excluded)
        fi

        [ "$DRY_RUN" = "1" ] || cdk_bootstrap

        SKIPPING=0
        [ -n "$FROM_MODULE" ] && SKIPPING=1
        for m in $SEQUENCE; do
            if [ "$SKIPPING" = "1" ]; then
                if [ "$m" = "$FROM_MODULE" ]; then SKIPPING=0
                else log_info "Skipping module $m (--from $FROM_MODULE)"; continue; fi
            fi
            # A2A sub-agent stacks only exist in the CDK app when enable_a2a=true.
            if [ "$m" = "8" ]; then export ENABLE_A2A=true; build_context_args; fi

            title="${MODULE_EXPLAIN[$m]%%$'\n'*}"
            log_header "Module $m — $title"
            log_explain "${MODULE_EXPLAIN[$m]#*$'\n'}"
            echo ""
            if [ "$DRY_RUN" = "1" ]; then
                log_info "[dry-run] Would deploy: ${MODULE_MAP[$m]}"
                log_info "[dry-run] Would verify: ${MODULE_VERIFY[$m]}"
                continue
            fi
            pause
            # shellcheck disable=SC2086  # stack list is space-separated by design
            deploy_stacks ${MODULE_MAP[$m]}
            run_verify "$m"
            pause
        done

        log_header "Workshop complete 🎉"
        [ "$DRY_RUN" = "1" ] || print_summary
        ;;

    destroy)
        log_header "Destroying"
        [ -z "${CDK_STACKS:-}" ] && confirm_footprint destroy
        # No stacks selected means --all. Failures are no longer swallowed: a
        # refused destroy is the guard against cascading, so it has to be seen.
        # shellcheck disable=SC2086  # stack list is space-separated by design
        destroy_stacks ${CDK_STACKS:-} || exit 1
        ;;

    verify)
        # One configuration-aware health check: asks the deployment contract
        # what this config promises and runs the matching tools; non-zero if
        # any claim fails. Details: scripts/verify.py
        "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/verify.py"
        ;;

    synth)
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk synth "${CONTEXT_ARGS[@]}" ${CDK_STACKS:+$CDK_STACKS} 2>&1
        ;;

    diff)
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk diff "${CONTEXT_ARGS[@]}" ${CDK_STACKS:+$CDK_STACKS} 2>&1
        ;;

    export)
        export_artifacts
        ;;

    ls|list)
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk ls "${CONTEXT_ARGS[@]}" 2>&1
        ;;

    *)
        echo "Usage: $0 [deploy|workshop|destroy|synth|diff|export|ls|config] [OPTIONS]"
        echo ""
        echo "Actions:"
        echo "  workshop           Guided module-by-module deploy: explain → deploy → verify → pause"
        echo "  verify             Run every check this configuration promises; non-zero on failure"
        echo "  config             Show saved answers (workshop.env)"
        echo "  config --reset     Delete saved answers and start fresh"
        echo ""
        echo "Options:"
        echo "  --stack STACK      Deploy specific stack"
        echo "  --profile PROFILE  Customer profile (greenfield|migration|multi-agent|platform-team|security-focused)"
        echo "  --team TEAM        Team workstream (platform|agent|security)"
        echo "  --module N         Workshop module number (3|4|5|6|7|8|9|A|B|C|D|E)"
        echo "  --from MODULE      (workshop) Start at this module in the profile sequence"
        echo "  --dry-run          (workshop) Print each module's stacks + verify command; no AWS calls"
        echo "  --yes              Skip the full-footprint confirmation (deploy/destroy with no target)"
        echo ""
        echo "Environment Variables:"
        echo "  NON_INTERACTIVE=1  Skip all prompts"
        echo "  PROJECT_NAME       Project name (default: agentcore-workshop)"
        echo "  ENVIRONMENT        Environment (default: dev)"
        echo "  AWS_REGION         AWS region"
        echo "  IDP_TYPE           Identity provider (cognito|entra_id|okta|ping)"
        echo "  MODEL_ID           Bedrock model ID override for all agents (default: in-code per pattern)"
        exit 1
        ;;
esac
