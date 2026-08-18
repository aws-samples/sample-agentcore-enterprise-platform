#!/usr/bin/env bash
# Self-check for deploy.sh workshop.env persistence (no AWS, no framework).
# (a) load_config respects env-var precedence  (b) save_config never writes
# secrets  (c) `deploy.sh config --reset` deletes the file.
set -euo pipefail
[ "${BASH_VERSINFO[0]:-0}" -ge 4 ] || { echo "needs bash 4+ (brew install bash)" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
log_info() { :; }

# Sandbox: pull ONLY the config vars + functions out of deploy.sh (no main flow).
# shellcheck disable=SC2034  # used by the eval'd CONFIG_FILE= line below
PROJECT_DIR="$TMP"
# The CONFIG_KEYS range ends at the first line closing the array, so adding a
# key to deploy.sh does not silently swallow the rest of the file here.
eval "$(sed -n '/^CONFIG_FILE=/p; /^CONFIG_KEYS=/,/)$/p;
                /^save_config()/,/^}/p; /^load_config()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
CFG="$TMP/workshop.env"
[ "$CONFIG_FILE" = "$CFG" ] || fail "CONFIG_FILE extraction broken: $CONFIG_FILE"

# (a) env var already set wins over the saved file; unset vars are filled in.
printf 'AWS_REGION=eu-central-1\nIDP_TYPE=okta\n' > "$CFG"  # pragma: allowlist secret
AWS_REGION="us-west-2"
load_config
[ "$AWS_REGION" = "us-west-2" ] || fail "env var clobbered by saved config"
[ "${IDP_TYPE:-}" = "okta" ]    || fail "saved value not loaded"
echo "PASS: env var wins over workshop.env; unset keys are loaded"

# (b) secrets in the environment never reach the file.
IDP_CLIENT_SECRET="supersecret" TAVILY_API_KEY="tv-key-123" save_config  # pragma: allowlist secret — test input proving secrets are never persisted
[ -f "$CFG" ] || fail "save_config wrote nothing"
! grep -v '^#' "$CFG" | grep -qiE 'secret|api_key|tv-key' || fail "secret leaked: $(cat "$CFG")"
grep -q '^AWS_REGION=us-west-2$' "$CFG" || fail "answer not persisted"
echo "PASS: secrets never written to workshop.env"

# NON_INTERACTIVE (CI) runs never write the file.
rm -f "$CFG"
NON_INTERACTIVE=1 save_config
[ ! -f "$CFG" ] || fail "NON_INTERACTIVE run wrote workshop.env"
echo "PASS: NON_INTERACTIVE never writes"

# (c) `config` prints, `config --reset` deletes (sandboxed copy of deploy.sh).
mkdir -p "$TMP/scripts"; cp "$SCRIPT_DIR/deploy.sh" "$TMP/scripts/"
printf 'AWS_REGION=us-west-2\n' > "$CFG"
bash "$TMP/scripts/deploy.sh" config | grep -q '^AWS_REGION=us-west-2$' \
    || fail "config action did not print saved file"
bash "$TMP/scripts/deploy.sh" config --reset >/dev/null
[ ! -f "$CFG" ] || fail "config --reset left the file behind"
bash "$TMP/scripts/deploy.sh" config | grep -q 'none' || fail "config after reset should say none"
echo "PASS: config / config --reset"

# (d) AGENT_PATTERN round-trips through workshop.env like any other answer.
AGENT_PATTERN="langgraph-agent" save_config
grep -q '^AGENT_PATTERN=langgraph-agent$' "$CFG" || fail "AGENT_PATTERN not persisted: $(cat "$CFG")"
unset AGENT_PATTERN
load_config
[ "${AGENT_PATTERN:-}" = "langgraph-agent" ] || fail "AGENT_PATTERN not restored"
AGENT_PATTERN="strands-agent"
load_config
[ "$AGENT_PATTERN" = "strands-agent" ] || fail "env AGENT_PATTERN clobbered by saved file"
echo "PASS: AGENT_PATTERN persists and env var still wins"

# (e) an unknown pattern fails fast with the valid list, before any AWS call.
rm -f "$CFG"
out="$(AGENT_PATTERN=bogus-agent bash "$TMP/scripts/deploy.sh" config 2>&1)" && \
    fail "unknown agent pattern was accepted"
grep -q 'bogus-agent' <<<"$out"      || fail "error does not name the bad pattern: $out"
grep -q 'langgraph-agent' <<<"$out"  || fail "error does not list valid patterns: $out"
AGENT_PATTERN=langgraph-agent bash "$TMP/scripts/deploy.sh" config >/dev/null \
    || fail "valid agent pattern rejected"
echo "PASS: unknown AGENT_PATTERN rejected with the valid list"

# (f) the pattern actually reaches CDK as a context flag (it used to rely on
# process env inheritance alone, which workshop.env could not restore).
eval "$(sed -n '/^build_context_args()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
PROJECT_NAME=p ENVIRONMENT=e AGENT_PATTERN=claude-sdk-agent build_context_args
[[ " ${CONTEXT_ARGS[*]} " == *" agent_pattern=claude-sdk-agent "* ]] \
    || fail "agent_pattern missing from CDK context args: ${CONTEXT_ARGS[*]}"
echo "PASS: agent_pattern passed to CDK as a context flag"

# (g) the CDK CLI probe never blocks on npx's install prompt. A hang here shows
# up as this check timing out instead of as a mystery in the field.
STUB="$TMP/stub"; mkdir -p "$STUB"
cat > "$STUB/npx" <<'STUB_NPX'
#!/usr/bin/env bash
# Models npx when aws-cdk is not in the cache: a bare call prompts and waits
# forever with no TTY; --no-install fails fast instead.
for a in "$@"; do [ "$a" = "--no-install" ] && { echo "npx: not found: cdk" >&2; exit 1; }; done
echo "Need to install the following packages: aws-cdk  Ok to proceed? (y)"
sleep 300
STUB_NPX
# Stubbed so the fallback records its arguments instead of really installing.
printf '#!/usr/bin/env bash\necho "$*" >> "%s/npm.args"\n' "$TMP" > "$STUB/npm"
chmod +x "$STUB/npx" "$STUB/npm"
log_header() { :; }; log_warn() { :; }; log_error() { :; }
eval "$(sed -n '/^check_prereqs()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"

PATH="$STUB:$PATH" check_prereqs >/dev/null 2>&1 & probe=$!
( sleep 15; kill -9 "$probe" 2>/dev/null ) & watchdog=$!
wait "$probe" && probe_rc=0 || probe_rc=$?
kill "$watchdog" 2>/dev/null || true
[ "$probe_rc" -ne 137 ] || fail "check_prereqs hung on the npx install prompt"
grep -q -- 'install -g aws-cdk' "$TMP/npm.args" \
    || fail "probe failed but the install fallback never ran: $(cat "$TMP/npm.args" 2>/dev/null)"
echo "PASS: CDK probe fails fast and falls back to installing, never prompts"

# (h) a targeted destroy is --exclusively (so CloudFormation refuses instead of
# cascading into dependent stacks), --all still cascades, and a refused destroy
# is reported instead of swallowed.
STUB2="$TMP/stub2"; mkdir -p "$STUB2"
cat > "$STUB2/npx" <<'STUB_CDK'
#!/usr/bin/env bash
# Records the CDK invocation; "boom" models a stack CloudFormation refuses.
echo "$*" >> "$CDK_ARGS"
for a in "$@"; do [ "$a" = boom ] && exit 1; done
exit 0
STUB_CDK
chmod +x "$STUB2/npx"
export CDK_ARGS="$TMP/cdk.args"
log_step() { :; }
CONTEXT_ARGS=()
eval "$(sed -n '/^destroy_stacks()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"

PATH="$STUB2:$PATH" destroy_stacks net-stack >/dev/null 2>&1 || fail "targeted destroy errored"
grep -q -- '--exclusively' "$TMP/cdk.args" \
    || fail "targeted destroy would cascade into dependent stacks: $(cat "$TMP/cdk.args")"
echo "PASS: targeted destroy passes --exclusively"

: > "$TMP/cdk.args"
PATH="$STUB2:$PATH" destroy_stacks >/dev/null 2>&1 || fail "destroy --all errored"
grep -q -- '--all' "$TMP/cdk.args" || fail "destroy with no target did not use --all"
! grep -q -- '--exclusively' "$TMP/cdk.args" || fail "--all must keep cascading"
echo "PASS: destroy --all still cascades"

PATH="$STUB2:$PATH" destroy_stacks boom >/dev/null 2>&1 && fail "refused destroy reported success"
echo "PASS: a refused destroy is not swallowed"

# (i) platform.yaml participates with the right precedence:
# explicit env > platform.yaml > workshop.env.
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
eval "$(sed -n '/^PLATFORM_CONFIG=/p; /^apply_platform_config()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
[ -n "$PLATFORM_CONFIG" ] || fail "apply_platform_config extraction broken"
# The sandbox PROJECT_DIR has no venv/infra_utils — point the loader at the repo.
PROJECT_DIR="$REPO_ROOT"
PLATFORM_CONFIG="$TMP/platform.yaml"
cat > "$PLATFORM_CONFIG" <<'YAML'
project: from-yaml
region: eu-west-1
agents:
  pattern: langgraph-agent
YAML
printf 'AGENT_PATTERN=strands-agent\nAWS_REGION=eu-central-1\n' > "$CFG"  # pragma: allowlist secret
unset PROJECT_NAME AGENT_PATTERN AWS_REGION 2>/dev/null || true
AWS_REGION="us-west-2"            # explicit env: must survive everything
apply_platform_config
load_config
[ "$AWS_REGION" = "us-west-2" ]        || fail "env var lost to platform.yaml: $AWS_REGION"
[ "$PROJECT_NAME" = "from-yaml" ]      || fail "platform.yaml value not applied: ${PROJECT_NAME:-unset}"
[ "$AGENT_PATTERN" = "langgraph-agent" ] || fail "workshop.env beat platform.yaml: $AGENT_PATTERN"
echo "PASS: env > platform.yaml > workshop.env precedence"

# (j) an invalid platform.yaml stops the run instead of deploying defaults.
printf 'agents:\n  pattern: skynet\n' > "$PLATFORM_CONFIG"
( apply_platform_config ) >/dev/null 2>&1 && fail "invalid platform.yaml was accepted"
echo "PASS: invalid platform.yaml refuses to continue"
rm -f "$PLATFORM_CONFIG"

echo "OK: all deploy-config checks passed"
