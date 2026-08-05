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
printf 'AWS_REGION=eu-central-1\nIDP_TYPE=okta\n' > "$CFG"
AWS_REGION="us-west-2"
load_config
[ "$AWS_REGION" = "us-west-2" ] || fail "env var clobbered by saved config"
[ "${IDP_TYPE:-}" = "okta" ]    || fail "saved value not loaded"
echo "PASS: env var wins over workshop.env; unset keys are loaded"

# (b) secrets in the environment never reach the file.
IDP_CLIENT_SECRET="supersecret" TAVILY_API_KEY="tv-key-123" save_config
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

echo "OK: all deploy-config checks passed"
