#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════
# AgentCore Workshop CDK Deploy Script
# ═══════════════════════════════════════════════════════════════
#
# Usage:
#   ./deploy.sh deploy [--stack STACK] [--profile PROFILE] [--team TEAM] [--module N]
#   ./deploy.sh destroy [--stack STACK]
#   ./deploy.sh synth
#   ./deploy.sh diff
#   ./deploy.sh export
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

# ── Configuration ──
PROJECT_NAME="${PROJECT_NAME:-agentcore-workshop}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"

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
MODULE_MAP[C]="${PREFIX}-networking"                                              # Multi-Account Mesh
MODULE_MAP[D]="${PREFIX}-security"                                                # CI/CD Pipeline
MODULE_MAP[E]="${PREFIX}-security"                                                # Security Automation

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

    # Check CDK CLI
    if npx cdk --version &>/dev/null 2>&1; then
        log_info "✓ cdk: $(npx cdk --version 2>/dev/null)"
    else
        log_warn "CDK CLI not found, installing..."
        npm install -g aws-cdk
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
    read -rp "Select region [1]: " choice
    case "${choice:-1}" in
        1) AWS_REGION="us-east-1" ;; 2) AWS_REGION="us-west-2" ;;
        3) AWS_REGION="eu-west-1" ;; 4) AWS_REGION="eu-central-1" ;;
        5) AWS_REGION="ap-northeast-1" ;; 6) AWS_REGION="ap-southeast-1" ;;
        *) AWS_REGION="us-east-1" ;;
    esac
    export CDK_DEFAULT_REGION="$AWS_REGION"
    log_info "Region set to: $AWS_REGION"
}

prompt_idp() {
    if [ "${NON_INTERACTIVE:-0}" = "1" ]; then return; fi
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

prompt_observability() {
    if [ "${NON_INTERACTIVE:-0}" = "1" ]; then return; fi
    echo ""
    echo "Observability Backend:"
    echo "  1) Amazon CloudWatch (default)"
    echo "  2) DataDog"
    read -rp "Select backend [1]: " choice
    case "${choice:-1}" in
        2) OBSERVABILITY_BACKEND="datadog"
           read -rp "  DataDog API Key: " DATADOG_API_KEY
           read -rp "  DataDog Site [datadoghq.com]: " DATADOG_SITE
           DATADOG_SITE="${DATADOG_SITE:-datadoghq.com}"
           ;;
        *) OBSERVABILITY_BACKEND="cloudwatch" ;;
    esac
    log_info "Observability backend: ${OBSERVABILITY_BACKEND}"
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
# Python Virtual Environment
# ═══════════════════════════════════════════════════════════════
setup_venv() {
    if [ ! -d "$PROJECT_DIR/.venv" ]; then
        log_step "Creating Python virtual environment..."
        python3.13 -m venv "$PROJECT_DIR/.venv"
    fi
    source "$PROJECT_DIR/.venv/bin/activate"
    pip install -q -r "$PROJECT_DIR/requirements.txt" 2>/dev/null
}

# ═══════════════════════════════════════════════════════════════
# CDK Context Builder
# ═══════════════════════════════════════════════════════════════
build_context_args() {
    local args=""
    args+=" -c project=${PROJECT_NAME}"
    args+=" -c environment=${ENVIRONMENT}"
    args+=" -c region=${AWS_REGION:-us-east-1}"
    args+=" -c idp_type=${IDP_TYPE:-cognito}"
    args+=" -c observability_backend=${OBSERVABILITY_BACKEND:-cloudwatch}"

    # IdP config
    [ -n "${IDP_TENANT_ID:-}" ]    && args+=" -c idp_tenant_id=${IDP_TENANT_ID}"
    [ -n "${IDP_CLIENT_ID:-}" ]    && args+=" -c idp_client_id=${IDP_CLIENT_ID}"
    [ -n "${IDP_CLIENT_SECRET:-}" ] && args+=" -c idp_client_secret=${IDP_CLIENT_SECRET}"
    [ -n "${IDP_ISSUER_URL:-}" ]   && args+=" -c idp_issuer_url=${IDP_ISSUER_URL}"

    # Feature flags from profile
    [ -n "${ENABLE_NETWORKING:-}" ] && args+=" -c enable_networking=${ENABLE_NETWORKING}"
    [ -n "${ENABLE_SECURITY:-}" ]   && args+=" -c enable_security=${ENABLE_SECURITY}"
    [ -n "${ENABLE_A2A:-}" ]        && args+=" -c enable_a2a=${ENABLE_A2A}"

    # Security control feature flags (control-library / scope-split model)
    [ -n "${ENABLE_RESOURCE_POLICIES:-}" ] && args+=" -c enable_resource_policies=${ENABLE_RESOURCE_POLICIES}"
    [ -n "${ENABLE_EGRESS_FILTER:-}" ]     && args+=" -c enable_egress_filter=${ENABLE_EGRESS_FILTER}"
    [ -n "${ENABLE_CEDAR:-}" ]             && args+=" -c enable_cedar=${ENABLE_CEDAR}"
    [ -n "${CEDAR_MODE:-}" ]               && args+=" -c cedar_mode=${CEDAR_MODE}"
    [ -n "${ENABLE_TRACEABILITY:-}" ]      && args+=" -c enable_traceability=${ENABLE_TRACEABILITY}"
    [ -n "${ORG_ID:-}" ]                   && args+=" -c org_id=${ORG_ID}"

    echo "$args"
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
    local export_file="${PROJECT_DIR}/workshop-outputs-$(date +%Y%m%d-%H%M%S).json"

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

# Parse flags
STACK_FILTER=""
PROFILE=""
TEAM=""
MODULE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stack)   STACK_FILTER="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --team)    TEAM="$2"; shift 2 ;;
        --module)  MODULE="$2"; shift 2 ;;
        *)         shift ;;
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

# Resolve stack targets
CDK_STACKS=""
if [ -n "$STACK_FILTER" ]; then
    CDK_STACKS="$STACK_FILTER"
elif [ -n "$MODULE" ] && [ -n "${MODULE_MAP[$MODULE]:-}" ]; then
    CDK_STACKS="${MODULE_MAP[$MODULE]}"
    log_info "Workshop Module $MODULE → Stacks: $CDK_STACKS"
elif [ -n "$TEAM" ] && [ -n "${TEAM_MAP[$TEAM]:-}" ]; then
    CDK_STACKS="${TEAM_MAP[$TEAM]}"
    log_info "Team $TEAM → Stacks: $CDK_STACKS"
fi

check_prereqs
check_credentials
setup_venv

cd "$PROJECT_DIR"

# Build CDK context args
CONTEXT_ARGS=$(build_context_args)

case "$ACTION" in
    deploy)
        if [ "${NON_INTERACTIVE:-0}" != "1" ]; then
            prompt_region
            prompt_idp
            prompt_observability
            prompt_api_keys
            CONTEXT_ARGS=$(build_context_args)  # Rebuild with new values
        fi

        log_header "CDK Bootstrap"
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk bootstrap "aws://$ACCOUNT_ID/$AWS_REGION" 2>/dev/null || true

        log_header "Deploying"
        if [ -n "$CDK_STACKS" ]; then
            for stack in $CDK_STACKS; do
                log_step "Deploying: $stack"
                JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
                    npx cdk deploy "$stack" --require-approval never $CONTEXT_ARGS 2>&1 || {
                    log_error "Failed to deploy $stack"
                    log_error "Check CloudFormation console for details."
                    exit 1
                }
            done
        else
            log_step "Deploying all stacks..."
            JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
                npx cdk deploy --all --require-approval never $CONTEXT_ARGS 2>&1 || {
                log_error "Deployment failed. Check CloudFormation console for details."
                exit 1
            }
        fi

        print_summary
        ;;

    destroy)
        log_header "Destroying"
        if [ -n "$CDK_STACKS" ]; then
            for stack in $CDK_STACKS; do
                log_step "Destroying: $stack"
                JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
                    npx cdk destroy "$stack" --force $CONTEXT_ARGS 2>&1 || true
            done
        else
            JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
                npx cdk destroy --all --force $CONTEXT_ARGS 2>&1 || true
        fi
        ;;

    synth)
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk synth $CONTEXT_ARGS ${CDK_STACKS:+$CDK_STACKS} 2>&1
        ;;

    diff)
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk diff $CONTEXT_ARGS ${CDK_STACKS:+$CDK_STACKS} 2>&1
        ;;

    export)
        export_artifacts
        ;;

    ls|list)
        JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 \
            npx cdk ls $CONTEXT_ARGS 2>&1
        ;;

    *)
        echo "Usage: $0 [deploy|destroy|synth|diff|export|ls] [OPTIONS]"
        echo ""
        echo "Options:"
        echo "  --stack STACK      Deploy specific stack"
        echo "  --profile PROFILE  Customer profile (greenfield|migration|multi-agent|platform-team|security-focused)"
        echo "  --team TEAM        Team workstream (platform|agent|security)"
        echo "  --module N         Workshop module number (3|4|5|6|7|8|9|A|B|C|D|E)"
        echo ""
        echo "Environment Variables:"
        echo "  NON_INTERACTIVE=1  Skip all prompts"
        echo "  PROJECT_NAME       Project name (default: agentcore-workshop)"
        echo "  ENVIRONMENT        Environment (default: dev)"
        echo "  AWS_REGION         AWS region"
        echo "  IDP_TYPE           Identity provider (cognito|entra_id|okta|ping)"
        exit 1
        ;;
esac
