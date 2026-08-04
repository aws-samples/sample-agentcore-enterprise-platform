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
eval "$(sed -n '/^CONFIG_FILE=/p; /^CONFIG_KEYS=/,/ENVIRONMENT)/p;
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

echo "OK: all deploy-config checks passed"
