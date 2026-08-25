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
# shellcheck disable=SC2034  # read by the eval'd apply_platform_config
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
# Unset (not just wrong) when the loader could not run at all — keep the guard
# tolerant of that so the message explains it instead of `set -u` aborting here.
[ "${PROJECT_NAME:-}" = "from-yaml" ] || fail "platform.yaml value not applied: ${PROJECT_NAME:-unset} (is pydantic installed?)"
[ "$AGENT_PATTERN" = "langgraph-agent" ] || fail "workshop.env beat platform.yaml: $AGENT_PATTERN"
echo "PASS: env > platform.yaml > workshop.env precedence"

# (j) an invalid platform.yaml stops the run instead of deploying defaults.
printf 'agents:\n  pattern: skynet\n' > "$PLATFORM_CONFIG"
( apply_platform_config ) >/dev/null 2>&1 && fail "invalid platform.yaml was accepted"
echo "PASS: invalid platform.yaml refuses to continue"
rm -f "$PLATFORM_CONFIG"

# (k) the IdP client secret is trimmed before it reaches Secrets Manager.
# A trailing newline (pasted, or piped from `az ... -o tsv`) is stored verbatim,
# Cognito forwards it to the IdP token endpoint, and the exchange fails with
# invalid_client mentioning nothing about whitespace.
eval "$(sed -n '/^upsert_oauth_secret()/,/^}/p; /^upsert_idp_secret()/,/^}/p; /^upsert_3lo_secrets()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
# The function unsets the plaintext when it is done (deliberate hygiene), so
# assert on what it PASSED to the CLI rather than on the variable afterwards.
# SC2034/SC2329: PREFIX, AWS_REGION and IDP_CLIENT_SECRET are read by the
# eval'd function, and the aws stub is invoked from inside it — both invisible
# to shellcheck's static view.
# shellcheck disable=SC2034,SC2329
aws() { printf '%s\n' "$*" >> "$TMP/aws.args"; return 0; }
# shellcheck disable=SC2034
PREFIX="check-prefix"; AWS_REGION="us-east-1"

: > "$TMP/aws.args"
IDP_CLIENT_SECRET=$'sekret-value\n'          # trailing newline, as pasted/piped
upsert_idp_secret >/dev/null 2>&1 || true
grep -q -- "--secret-string sekret-value " "$TMP/aws.args" \
    || fail "newline not stripped before Secrets Manager: $(cat "$TMP/aws.args")"

: > "$TMP/aws.args"
IDP_CLIENT_SECRET="  padded  "
upsert_idp_secret >/dev/null 2>&1 || true
grep -q -- "--secret-string padded " "$TMP/aws.args" \
    || fail "surrounding spaces not stripped: $(cat "$TMP/aws.args")"

# All-whitespace must stop the run rather than store an empty secret.
# shellcheck disable=SC2034
IDP_CLIENT_SECRET=$'\n  \n'
( upsert_idp_secret ) >/dev/null 2>&1 && fail "whitespace-only secret was accepted"
unset -f aws
echo "PASS: IdP client secret is trimmed (and empty-after-trim refused)"

# (l) a secret the operator already owns is used, not duplicated under our name.
# Configuring identity.client_secret_name (platform.yaml) or IDP_CLIENT_SECRET_NAME
# used to be ignored: deploy.sh probed only <prefix>-idp-client-secret, asked for
# the value anyway, and stored a second copy.
eval "$(sed -n '/^prompt_idp()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
# shellcheck disable=SC2034,SC2329  # read by the eval'd functions, not statically
NON_INTERACTIVE=0
IDP_CLIENT_SECRET_NAME="my-corp/entra-secret"
IDP_TYPE="entra_id"
unset IDP_CLIENT_SECRET

# The stub answers "exists" only for the operator's secret, so probing the
# prefixed name instead would fall through to the secret prompt.
# shellcheck disable=SC2329
aws() { printf '%s\n' "$*" >> "$TMP/aws.args"; [[ "$*" == *my-corp/entra-secret* ]]; }
: > "$TMP/aws.args"
# Two blank lines: "keep saved IdP? [Y/n]" → yes, plus a spare for the secret
# prompt the old behaviour reached — so this check fails with its own message
# rather than dying on `read` at EOF.
prompt_idp >/dev/null 2>&1 <<< $'\n'
grep -q -- "describe-secret --secret-id my-corp/entra-secret" "$TMP/aws.args" \
    || fail "configured secret name not probed: $(cat "$TMP/aws.args")"
[ "$IDP_CLIENT_SECRET_NAME" = "my-corp/entra-secret" ] \
    || fail "configured secret name overwritten: $IDP_CLIENT_SECRET_NAME"

# Rotating a value in must land in the operator's secret, not a fork of it.
# shellcheck disable=SC2329
aws() { printf '%s\n' "$*" >> "$TMP/aws.args"; return 0; }
: > "$TMP/aws.args"
# shellcheck disable=SC2034  # read by the eval'd upsert_idp_secret
IDP_CLIENT_SECRET="rotated-value"
upsert_idp_secret >/dev/null 2>&1 || true
grep -q -- "put-secret-value --secret-id my-corp/entra-secret" "$TMP/aws.args" \
    || fail "rotation did not target the configured secret: $(cat "$TMP/aws.args")"
! grep -q -- "$PREFIX-idp-client-secret" "$TMP/aws.args" \
    || fail "secret duplicated under our own name: $(cat "$TMP/aws.args")"
unset -f aws prompt_idp
echo "PASS: a configured IdP secret name is reused, not duplicated"

# (m) 3LO client secrets follow the same road: trimmed, stored under the
# prefixed name (or a configured one), plaintext unset afterwards. These used
# to be rendered verbatim into the synthesized template via cdk context.
# shellcheck disable=SC2329
aws() { printf '%s\n' "$*" >> "$TMP/aws.args"; return 0; }
: > "$TMP/aws.args"
# shellcheck disable=SC2034  # read via indirection in the eval'd functions
GOOGLE_CLIENT_SECRET=$'g-sekret\n'
# shellcheck disable=SC2034
GITHUB_CLIENT_SECRET="  gh-sekret  "
# shellcheck disable=SC2034
NOTION_CLIENT_SECRET_NAME="my-corp/notion"   # bring-your-own name
# shellcheck disable=SC2034
NOTION_CLIENT_SECRET="n-sekret"
upsert_3lo_secrets >/dev/null 2>&1 || true
grep -q -- "--secret-string g-sekret " "$TMP/aws.args" \
    || fail "google secret newline not stripped: $(cat "$TMP/aws.args")"
grep -q -- "--secret-string gh-sekret " "$TMP/aws.args" \
    || fail "github secret padding not stripped: $(cat "$TMP/aws.args")"
grep -q -- "--secret-id my-corp/notion" "$TMP/aws.args" \
    || fail "notion bring-your-own name ignored: $(cat "$TMP/aws.args")"
[ "$GOOGLE_CLIENT_SECRET_NAME" = "check-prefix-google-oauth-secret" ] \
    || fail "google secret name not defaulted: ${GOOGLE_CLIENT_SECRET_NAME:-unset}"
[ -z "${GOOGLE_CLIENT_SECRET:-}${GITHUB_CLIENT_SECRET:-}${NOTION_CLIENT_SECRET:-}" ] \
    || fail "a 3LO plaintext survived the upsert"
unset -f aws
echo "PASS: 3LO client secrets are trimmed, named, and never persisted"

# (n) invalid CLI input fails closed, before any AWS call. A typo'd option
# used to be silently discarded; with no resolved target the run escalated to
# `cdk deploy --all --require-approval never`. These invoke the REAL script:
# every rejection below happens at parse/validation time, pre-credentials.
run_deploy() { (cd "$REPO_ROOT" && "$BASH" scripts/deploy.sh "$@") }

out=$(run_deploy deploy --bogus-flag 2>&1) && fail "unknown option was accepted"
echo "$out" | grep -q -- "--bogus-flag" || fail "unknown option not named: $out"

out=$(run_deploy deploy --stack=identity 2>&1) && fail "--stack=NAME form was accepted"

out=$(run_deploy deploy --profile greenfied 2>&1) && fail "misspelled profile was accepted"
echo "$out" | grep -q "greenfield" || fail "valid profiles not listed: $out"

out=$(run_deploy deploy --team agents 2>&1) && fail "unknown team was accepted"

out=$(run_deploy deploy --stack identity 2>&1) && fail "short stack name was accepted"
echo "$out" | grep -q -- "-identity" || fail "full-name hint missing: $out"

# --yes must parse as a flag (not hit the unknown-option arm): with it present
# the run must get PAST parsing and die on the misspelled profile instead.
out=$(run_deploy deploy --yes --profile greenfied 2>&1) && fail "--yes+bad profile accepted"
echo "$out" | grep -q "Unknown profile" || fail "--yes not parsed as a flag: $out"
echo "PASS: invalid CLI input fails closed before any AWS call"

# (o) the full-footprint gate: --yes and NON_INTERACTIVE skip it; an answer
# of anything but y aborts with a non-zero exit and no mutation.
eval "$(sed -n '/^confirm_footprint()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
log_header() { :; }; log_warn() { :; }; log_error() { :; }
# shellcheck disable=SC2329  # invoked from inside the eval'd function
npx() { echo "stack-a"; echo "stack-b"; }
CONTEXT_ARGS=(); PLATFORM_CONFIG="$TMP/absent.yaml"
# shellcheck disable=SC2034  # YES/NON_INTERACTIVE are read by the eval'd function
YES=1                 && confirm_footprint deploy  || fail "--yes did not skip the gate"
YES=0 NON_INTERACTIVE=1 confirm_footprint deploy   || fail "NON_INTERACTIVE did not imply --yes"
# shellcheck disable=SC2034
NON_INTERACTIVE=0
( YES=0 confirm_footprint destroy <<< "n" ) >/dev/null 2>&1 && fail "answering n did not abort"
( YES=0 confirm_footprint destroy <<< "y" ) >/dev/null 2>&1 || fail "answering y did not proceed"
unset -f npx
echo "PASS: full-footprint gate honors --yes / NON_INTERACTIVE and aborts on n"

# (p) --profile materializes its preset as platform.yaml — durable intent.
# The old PROFILE_FLAGS env exports died with the run, so a later plain
# deploy silently changed the footprint (A2A defaulted back on).
eval "$(sed -n '/^MATERIALIZE_HEADER=/p; /^valid_profiles()/,/^}/p; /^materialize_preset()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
log_error() { echo "$*"; }   # (o) muted it; (p) asserts on the messages
# shellcheck disable=SC2034  # read by the eval'd materialize_preset
PROJECT_DIR="$TMP"
PLATFORM_CONFIG="$TMP/platform.yaml"
mkdir -p "$TMP/presets"
printf 'project: from-preset\nagents:\n  a2a: false\n' > "$TMP/presets/small.yaml"

materialize_preset small >/dev/null 2>&1 || fail "materialize failed on a fresh tree"
head -1 "$PLATFORM_CONFIG" | grep -q "Generated from presets/small" \
    || fail "generated header missing: $(head -1 "$PLATFORM_CONFIG")"
grep -q "project: from-preset" "$PLATFORM_CONFIG" || fail "preset content not copied"

# Re-running the same profile regenerates without complaint.
materialize_preset small >/dev/null 2>&1 || fail "regeneration of a generated file refused"

# A hand-edited manifest (no header) is refused without --yes...
printf 'project: hand-edited\n' > "$PLATFORM_CONFIG"
( PRESCAN_YES=0 materialize_preset small ) >/dev/null 2>&1 \
    && fail "hand-edited platform.yaml was clobbered"
grep -q "hand-edited" "$PLATFORM_CONFIG" || fail "refusal still modified the file"
# ...and overwritten with it.
( PRESCAN_YES=1 materialize_preset small ) >/dev/null 2>&1 \
    || fail "--yes did not allow the overwrite"

# Unknown profile: non-zero, and the valid list names the real presets.
out=$( (materialize_preset nope) 2>&1 ) && fail "unknown profile accepted"
echo "$out" | grep -q "small" || fail "valid profiles not listed: $out"
rm -f "$PLATFORM_CONFIG"
echo "PASS: --profile materializes presets; hand-edits are protected"

# (q) the post-destroy sweep finds what the config cannot see, and deletes
# only when told to: --yes deletes, NON_INTERACTIVE without --yes only warns.
eval "$(sed -n '/^sweep_leftovers()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
# shellcheck disable=SC2329  # invoked from inside the eval'd function
aws() {
    printf '%s\n' "$*" >> "$TMP/aws.args"
    case "$1 $2" in
        "cloudformation list-stacks")   echo "check-prefix-networking" ;;
        "secretsmanager describe-secret")
            [[ "$*" == *check-prefix-idp-client-secret* ]] ;;
        *) return 0 ;;
    esac
}
# shellcheck disable=SC2034
AWS_REGION="us-east-1"

: > "$TMP/aws.args"
YES=1 NON_INTERACTIVE=0 sweep_leftovers >/dev/null 2>&1 || fail "sweep failed with --yes"
grep -q "delete-stack --stack-name check-prefix-networking" "$TMP/aws.args" \
    || fail "leftover stack not deleted with --yes: $(cat "$TMP/aws.args")"
grep -q "delete-secret --secret-id check-prefix-idp-client-secret" "$TMP/aws.args" \
    || fail "orphaned secret not deleted with --yes: $(cat "$TMP/aws.args")"

: > "$TMP/aws.args"
YES=0 NON_INTERACTIVE=1 sweep_leftovers >/dev/null 2>&1 || fail "sweep failed non-interactive"
grep -q "delete-stack\|delete-secret" "$TMP/aws.args" \
    && fail "NON_INTERACTIVE without --yes deleted things: $(cat "$TMP/aws.args")"
unset -f aws
echo "PASS: post-destroy sweep reports always, deletes only with --yes"

# (r) PROFILE_FLAGS must stay dead — resurrecting the env-export table brings
# back the drift this whole change removed.
! grep -q "PROFILE_FLAGS\[" "$SCRIPT_DIR/deploy.sh" \
    || fail "PROFILE_FLAGS table is back in deploy.sh"
echo "PASS: profile intent lives only in presets"

# (s) selecting a module/team also enables the feature flags its stacks need.
# `--module C` names ${PREFIX}-networking, but that stack only synthesizes
# when ENABLE_NETWORKING=true — cdk used to fail with "No stacks match" after
# bootstrap had already run. Explicit env must still win over the selection.
eval "$(sed -n '/^declare -A MODULE_FLAGS/p; /^MODULE_FLAGS\[/p;
                /^declare -A TEAM_FLAGS/p; /^TEAM_FLAGS\[/p;
                /^apply_flags()/,/^}/p; /^apply_module_flags()/p;
                /^apply_selection_flags()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
[ -n "${MODULE_FLAGS[C]:-}" ] || fail "MODULE_FLAGS extraction broken"
# shellcheck disable=SC2329  # invoked from inside the eval'd helper
build_context_args() { :; }

unset ENABLE_NETWORKING ENABLE_SECURITY ENABLE_A2A 2>/dev/null || true
# shellcheck disable=SC2034  # MODULE/TEAM are read by the eval'd apply_selection_flags
declare MODULE="C" TEAM=""
apply_selection_flags
[ "${ENABLE_NETWORKING:-}" = "true" ] || fail "module C did not enable ENABLE_NETWORKING"

unset ENABLE_NETWORKING
ENABLE_NETWORKING="false"       # explicit env: must survive the selection
apply_selection_flags
[ "$ENABLE_NETWORKING" = "false" ] || fail "explicit ENABLE_NETWORKING=false lost to module C"

unset ENABLE_A2A 2>/dev/null || true
apply_module_flags 8            # as the workshop loop applies it, per module
[ "${ENABLE_A2A:-}" = "true" ] || fail "module 8 did not enable ENABLE_A2A"

unset ENABLE_A2A
# shellcheck disable=SC2034
declare MODULE="" TEAM="agent"
apply_selection_flags
[ "${ENABLE_A2A:-}" = "true" ] || fail "team agent did not enable ENABLE_A2A"

unset ENABLE_NETWORKING ENABLE_SECURITY ENABLE_A2A 2>/dev/null || true
apply_module_flags 3            # no MODULE_FLAGS entry: must export nothing
[ -z "${ENABLE_NETWORKING:-}${ENABLE_SECURITY:-}${ENABLE_A2A:-}" ] \
    || fail "module 3 exported flags it does not need"
echo "PASS: module/team selection enables the flags its stacks need"

echo "OK: all deploy-config checks passed"
