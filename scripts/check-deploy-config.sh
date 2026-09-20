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

grep -q '^export AWS_PAGER=""$' "$SCRIPT_DIR/deploy.sh" \
    || fail "deploy.sh must disable the interactive AWS CLI pager"
echo "PASS: deploy output cannot be trapped by the AWS CLI pager"

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
eval "$(sed -n '/^prompt_idp()/,/^}/p;
                /^validate_configured_idp_secret()/,/^}/p;
                /^validate_deployment_account()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
log_error() { printf '%s\n' "$*" >&2; }
# shellcheck disable=SC2034,SC2329  # read by the eval'd functions, not statically
NON_INTERACTIVE=0
IDP_CLIENT_SECRET_NAME="my-corp/entra-secret"
IDP_TYPE="entra_id"
unset IDP_CLIENT_SECRET

# The stub returns only the non-sensitive SecretString length. The deploy must
# prove the configured value readable before prompting, then reuse its name.
# shellcheck disable=SC2329
aws() {
    printf '%s\n' "$*" >> "$TMP/aws.args"
    if [[ "$*" == *"get-secret-value"* && "$*" == *my-corp/entra-secret* ]]; then
        printf '12\n'
        return 0
    fi
    return 1
}
: > "$TMP/aws.args"
ACCOUNT_ID="111122223333"
AWS_REGION="us-east-1"
# shellcheck disable=SC2034  # read by the eval'd validation function
DEPLOYMENT_STRATEGY="centralized"
PLATFORM_ACCOUNT="$ACCOUNT_ID"
validate_configured_idp_secret >/dev/null
prompt_out="$(prompt_idp 2>&1 <<< $'\n')"
grep -q -- "get-secret-value --secret-id my-corp/entra-secret" "$TMP/aws.args" \
    || fail "configured secret value not validated: $(cat "$TMP/aws.args")"
! grep -q -- "Client Secret:" <<<"$prompt_out" \
    || fail "configured secret unexpectedly caused a plaintext prompt: $prompt_out"
[ "$IDP_CLIENT_SECRET_NAME" = "my-corp/entra-secret" ] \
    || fail "configured secret name overwritten: $IDP_CLIENT_SECRET_NAME"

# A valid identity for another account must stop before any deployment-side
# operation, even in --yes/non-interactive modes.
ACCOUNT_ID="999988887777"
PLATFORM_ACCOUNT="111122223333"
YES=1
NON_INTERACTIVE=1
account_out="$(validate_deployment_account 2>&1)" && \
    fail "wrong account was accepted with non-interactive confirmation bypasses"
grep -q -- "$ACCOUNT_ID" <<<"$account_out" \
    || fail "account mismatch does not name the active account: $account_out"
grep -q -- "$PLATFORM_ACCOUNT" <<<"$account_out" \
    || fail "account mismatch does not name the pinned account: $account_out"

# A configured name is authoritative. Missing/denied access must stop rather
# than falling through to a value prompt that could copy it into the wrong
# account.
ACCOUNT_ID="111122223333"
PLATFORM_ACCOUNT="$ACCOUNT_ID"
YES=0
NON_INTERACTIVE=0
# shellcheck disable=SC2329
aws() { printf '%s\n' "$*" >> "$TMP/aws.args"; return 254; }
: > "$TMP/aws.args"
secret_out="$(validate_configured_idp_secret 2>&1)" && \
    fail "unreadable configured IdP secret was accepted"
for fact in "$IDP_CLIENT_SECRET_NAME" "$ACCOUNT_ID" "$AWS_REGION"; do
    grep -q -- "$fact" <<<"$secret_out" \
        || fail "configured-secret error omits '$fact': $secret_out"
done

# With no configured secret, interactive entry is still supported but empty
# input is rejected locally.
unset IDP_CLIENT_SECRET_NAME IDP_CLIENT_SECRET
empty_out="$(prompt_idp 2>&1 <<< $'\n\n')" && \
    fail "empty interactive IdP secret was accepted"
grep -q 'empty IdP client secret' <<<"$empty_out" \
    || fail "empty-secret error is not actionable: $empty_out"

# Rotating a value in must land in the operator's secret, not a fork of it.
# shellcheck disable=SC2329
aws() { printf '%s\n' "$*" >> "$TMP/aws.args"; return 0; }
: > "$TMP/aws.args"
# shellcheck disable=SC2034  # read by the eval'd upsert_idp_secret
IDP_CLIENT_SECRET_NAME="my-corp/entra-secret"
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
# Stub for the eval'd apply_flags. Defined via eval so shellcheck 0.9 does
# not pair it with the earlier (also eval'd) real build_context_args call and
# raise SC2218 "defined later".
eval 'build_context_args() { :; }'

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

# (s) the region prompt precedes the first secret upsert in the main flow.
# upsert_* stores the secret region-scoped and unsets the plaintext, so a
# region prompt after it strands the secret in the provisional region with
# no value left to re-store (usability review, finding 7). Source-order
# assertion: the first CALL site of prompt_region must come before the first
# CALL site of upsert_idp_secret.
first_call_line() {  # $1: function name — first call site, definitions excluded
    grep -nE "^[^#]*\b$1\b" "$SCRIPT_DIR/deploy.sh" \
        | grep -v "$1()" | head -1 | cut -d: -f1
}
region_line=$(first_call_line prompt_region)
upsert_line=$(first_call_line upsert_idp_secret)
[ -n "$region_line" ] && [ -n "$upsert_line" ] \
    || fail "could not locate prompt_region/upsert_idp_secret call sites"
[ "$region_line" -lt "$upsert_line" ] \
    || fail "prompt_region (line $region_line) runs after upsert_idp_secret (line $upsert_line): a region change would strand the secret"
echo "PASS: region is final before the first secret upsert"

# (t) `migrate plan` renders the migration contract and only READS AWS:
# adapter mapping + secret names printed, found/missing per secret from a
# stubbed describe-secret, no mutating call, and any other sub-action fails
# closed. Real script, real contract; the aws CLI is a PATH stub.
STUB3="$TMP/stub3"; mkdir -p "$STUB3"
cat > "$STUB3/aws" <<'STUB_AWS'
#!/usr/bin/env bash
echo "$*" >> "$AWS_ARGS"
case "$1 $2" in
    "sts get-caller-identity")       echo 111111111111 ;;
    "configure get")                 echo us-east-1 ;;
    "secretsmanager describe-secret") [[ "$*" == *JIRA_TOKEN* ]] ;;
    *) exit 0 ;;
esac
STUB_AWS
chmod +x "$STUB3/aws"
export AWS_ARGS="$TMP/aws.args"
MIG_YAML="$TMP/migration.yaml"
cat > "$MIG_YAML" <<'YAML'
project: mig-check
migration:
  source:
    platform: openshift
    image: registry.example.com/team/agent:1.4.2
    port: 8000
    invoke_path: /run
    health_path: /healthz
    trigger: webhook
    secrets: [JIRA_TOKEN, GIT_TOKEN]
YAML
run_migrate() {
    (cd "$REPO_ROOT" && PATH="$STUB3:$PATH" PLATFORM_CONFIG="$MIG_YAML" \
        "$BASH" scripts/deploy.sh migrate "$@")
}
: > "$AWS_ARGS"
out=$(run_migrate plan 2>&1) || fail "migrate plan failed: $out"
grep -q "POST /invocations -> http://127.0.0.1:8000/run" <<<"$out" \
    || fail "adapter mapping not printed: $out"
grep -q "mig-check/dev/migration/JIRA_TOKEN" <<<"$out" || fail "secret name not printed: $out"
grep -q "found:.*JIRA_TOKEN" <<<"$out"   || fail "existing secret not reported found: $out"
grep -q "missing:.*GIT_TOKEN" <<<"$out"  || fail "absent secret not reported missing: $out"
grep -q "arm64" <<<"$out"                || fail "arm64 warning missing for a pre-built image: $out"
! grep -vE "get-caller-identity|configure get|describe-secret" "$AWS_ARGS" | grep -q . \
    || fail "migrate plan made a non-read AWS call: $(cat "$AWS_ARGS")"

out=$(run_migrate bogus 2>&1) && fail "unknown migrate sub-action was accepted"
grep -q "bogus" <<<"$out" || fail "bad sub-action not named: $out"
out=$(run_migrate plan --stack x 2>&1) && fail "migrate accepted a deploy option"
echo "PASS: migrate plan renders the contract, reads AWS only, fails closed otherwise"

# (u) Design → Build → Verify glue. `design --profile X` materializes the
# preset and prints the plan without deploying; a preset with placeholders is
# refused with the field named and the plan is NOT printed; `build` is `deploy`
# (reaches the same flow — here, the credentials gate under the stub); `usecase`
# routes to scripts/usecase.py. Same stub aws, no mutating call anywhere.
DESIGN_DIR="$TMP/design"; mkdir -p "$DESIGN_DIR"
run_design() {
    (cd "$REPO_ROOT" && PATH="$STUB3:$PATH" PLATFORM_CONFIG="$DESIGN_DIR/platform.yaml" \
        "$BASH" scripts/deploy.sh "$@")
}
out=$(run_design design 2>&1) && fail "design without a manifest succeeded"
grep -q "design --profile greenfield" <<<"$out" || fail "design did not point at a profile: $out"
: > "$AWS_ARGS"
out=$(run_design design --profile greenfield 2>&1) || fail "design --profile greenfield failed: $out"
head -1 "$DESIGN_DIR/platform.yaml" | grep -q "Generated from presets/greenfield" \
    || fail "design did not materialize the preset"
grep -q "Stacks (6):" <<<"$out" || fail "design plan missing the stack count: $out"
grep -q "Nothing has been deployed" <<<"$out" || fail "design did not say nothing was deployed: $out"
! grep -vE "get-caller-identity|configure get" "$AWS_ARGS" | grep -q . \
    || fail "design made a non-read AWS call: $(cat "$AWS_ARGS")"
rm -f "$DESIGN_DIR/platform.yaml"
out=$(run_design design --profile migration 2>&1) && fail "design accepted the migration preset's placeholders"
grep -q "identity.tenant_id is a placeholder" <<<"$out" || fail "placeholder not named: $out"
! grep -q "Stacks (" <<<"$out" || fail "design printed a plan for an undeployable manifest"
out=$(run_design design --stack x 2>&1) && fail "design accepted a deploy option"
out=$(run_design usecase list 2>&1) || fail "usecase list failed: $out"
grep -q "hello-platform" <<<"$out" || fail "usecase list did not reach scripts/usecase.py: $out"
printf 'project: glue-check\n' > "$DESIGN_DIR/platform.yaml"
out=$(NON_INTERACTIVE=1 run_design build --dry-run 2>&1) || true
grep -q "Credentials\|credentials" <<<"$out" || fail "build did not route into the deploy flow: $out"
echo "PASS: design materializes + plans without deploying, refuses placeholders; build = deploy; usecase routes"

# (v) switching a live platform to identity.mode direct deploys the auth
# CONSUMERS before auth: they import Cognito values as CloudFormation exports
# and CloudFormation refuses to update auth while an export is imported (hit
# live: "Cannot delete export ... in use by gateway, identity, runtime").
# Source-order assertion: the switch runs after the footprint confirmation
# and before the --all deploy; it excludes exactly the auth stack; and the
# mode reaches CDK as context.
# There is also a targeted-auth call in deploy_stacks; the full-deploy call is
# the last call site and is the one constrained by confirm_footprint/--all.
switch_line=$(grep -nE '^[^#]*\bswitch_issuer_consumers_first\b' "$SCRIPT_DIR/deploy.sh" \
    | grep -v 'switch_issuer_consumers_first()' | tail -1 | cut -d: -f1 || true)
all_line=$(grep -n 'npx cdk deploy --all' "$SCRIPT_DIR/deploy.sh" | head -1 | cut -d: -f1 || true)
confirm_line=$(grep -n 'confirm_footprint deploy' "$SCRIPT_DIR/deploy.sh" | head -1 | cut -d: -f1 || true)
[ -n "$switch_line" ] && [ -n "$all_line" ] && [ -n "$confirm_line" ] \
    || fail "could not locate the issuer-switch call site"
[ "$confirm_line" -lt "$switch_line" ] && [ "$switch_line" -lt "$all_line" ] \
    || fail "switch_issuer_consumers_first (line $switch_line) must run after confirm_footprint ($confirm_line) and before cdk deploy --all ($all_line)"
grep -q -- 'grep -v -- "-auth\$"' "$SCRIPT_DIR/deploy.sh" \
    || fail "the issuer switch must exclude exactly the auth stack"
grep -q -- '-c "idp_mode=\${IDP_MODE:-brokered}"' "$SCRIPT_DIR/deploy.sh" \
    || fail "IDP_MODE is not passed to CDK as idp_mode context"
echo "PASS: brokered → direct switch deploys auth consumers first; idp_mode reaches CDK"

# (w) the one-time compatibility deploy retains a secret-bearing output long
# enough to move Identity away from it. CDK prints outputs after deployment;
# prove the stream filter removes a seeded value before it reaches logs.
eval "$(sed -n '/^redact_legacy_m2m_output()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
seeded_secret="seeded-secret-that-must-not-reach-logs"
filtered="$(printf '%s\n' \
    "stack.ExportsOutputFnGetAttUserPoolM2MClientDescribeCognitoUserPoolClientABCUserPoolClientClientSecretXYZ = $seeded_secret" \
    | redact_legacy_m2m_output)"
! grep -q "$seeded_secret" <<<"$filtered" \
    || fail "legacy M2M output redaction exposed the seeded secret"
grep -q '\[REDACTED\]' <<<"$filtered" \
    || fail "legacy M2M output redaction did not mark the filtered value"
echo "PASS: legacy M2M compatibility output is redacted from deployment logs"

# (x) rotation checkpoints are optional only when SSM confirms they do not
# exist. An SSM/API failure must stop the deployment rather than silently
# restarting a credential rotation from an inconsistent phase.
# shellcheck disable=SC2329
log_error() {
    :
}
eval "$(sed -n '/^read_optional_rotation_parameter()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"

# shellcheck disable=SC2329
aws() {
    printf 'None\n'
}
rotation_value="$(read_optional_rotation_parameter '/test/missing')" \
    || fail "a confirmed-missing rotation checkpoint should be optional"
[ -z "$rotation_value" ] \
    || fail "a confirmed-missing rotation checkpoint should return an empty value"

# shellcheck disable=SC2329
aws() {
    return 42
}
if read_optional_rotation_parameter '/test/unavailable' >/dev/null 2>&1; then
    fail "an SSM failure must not be treated as a missing rotation checkpoint"
fi
echo "PASS: rotation checkpoint reads distinguish missing values from SSM failures"

# (y) mutating deployments use an atomic, owner-checked SSM lock. Exercise the
# extracted implementation with a file-backed AWS stub: one owner acquires and
# releases, and another owner cannot take over any existing lock implicitly.
eval "$(sed -n '/^release_m2m_deployment_lock()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
eval "$(sed -n '/^acquire_m2m_deployment_lock()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
LOCK_STATE="$TMP/m2m-deployment-lock"
PROJECT_NAME="lock-check"
# shellcheck disable=SC2034  # consumed by the function extracted with eval
ENVIRONMENT="dev"
AWS_REGION="us-east-1"
# shellcheck disable=SC2034  # consumed by the function extracted with eval
ACCOUNT_ID="111111111111"
M2M_DEPLOYMENT_LOCK_VALUE=""
M2M_DEPLOYMENT_LOCK_PARAMETER=""

# shellcheck disable=SC2329
aws() {
    local operation="${1:-} ${2:-}" arg value=""
    case "$operation" in
        "ssm get-parameters")
            if [ -f "$LOCK_STATE" ]; then
                cat "$LOCK_STATE"
            else
                printf 'None\n'
            fi
            ;;
        "ssm put-parameter")
            [ ! -f "$LOCK_STATE" ] || return 1
            while [ "$#" -gt 0 ]; do
                arg="$1"; shift
                if [ "$arg" = "--value" ]; then value="${1:-}"; break; fi
            done
            printf '%s' "$value" > "$LOCK_STATE"
            ;;
        "ssm delete-parameter")
            rm -f "$LOCK_STATE"
            ;;
        *)
            return 1
            ;;
    esac
}

acquire_m2m_deployment_lock
[ -s "$LOCK_STATE" ] || fail "deployment lock was not persisted"
owner="$M2M_DEPLOYMENT_LOCK_VALUE"
if (
    M2M_DEPLOYMENT_LOCK_VALUE=""
    # shellcheck disable=SC2034  # consumed by the function extracted with eval
    M2M_DEPLOYMENT_LOCK_PARAMETER=""
    acquire_m2m_deployment_lock
) >/dev/null 2>&1; then
    fail "a concurrent deployment acquired an active lock"
fi
[ "$(cat "$LOCK_STATE")" = "$owner" ] \
    || fail "a rejected concurrent deployment changed lock ownership"
release_m2m_deployment_lock
[ ! -e "$LOCK_STATE" ] || fail "deployment lock was not released by its owner"

printf 'owner-requiring-review\n' > "$LOCK_STATE"
if (
    M2M_DEPLOYMENT_LOCK_VALUE=""
    # shellcheck disable=SC2034  # consumed by the function extracted with eval
    M2M_DEPLOYMENT_LOCK_PARAMETER=""
    acquire_m2m_deployment_lock
) >/dev/null 2>&1; then
    fail "an existing deployment lock was taken over without owner review"
fi
[ "$(cat "$LOCK_STATE")" = "owner-requiring-review" ] \
    || fail "a rejected lock takeover changed the existing owner"
rm -f "$LOCK_STATE"
echo "PASS: deployment lock is atomic, owner-checked, and stale state fails closed"

# (z) deployment summaries and workshop exports use the same allow-list as the
# dashboard. Seed both a reviewed value and a legacy secret-bearing output;
# only the reviewed field may survive filtering.
# shellcheck disable=SC2034  # consumed by the function extracted with eval
PROJECT_DIR="$SCRIPT_DIR/.."
eval "$(sed -n '/^sanitize_public_metadata()/,/^}/p' "$SCRIPT_DIR/deploy.sh")"
export_seed="legacy-generated-secret-must-not-export"
safe_export="$(printf '%s' \
    "[{\"OutputKey\":\"GatewayUrl\",\"OutputValue\":\"https://safe.example\"},{\"OutputKey\":\"ExportsOutputFnGetAttUserPoolM2MClientClientSecret\",\"OutputValue\":\"$export_seed\"}]" \
    | sanitize_public_metadata stack-export gateway)"
grep -q 'https://safe.example' <<<"$safe_export" \
    || fail "reviewed stack output was removed from the safe export"
! grep -q "$export_seed" <<<"$safe_export" \
    || fail "legacy M2M secret survived the safe export filter"
handoff_export="$(printf '%s' \
    '[{"OutputKey":"M2MClientIdV2Export","OutputValue":"replacement-client"},{"OutputKey":"M2MClientSecretNameV2Export","OutputValue":"replacement-secret-name"}]' \
    | sanitize_public_metadata stack-export auth)"
grep -q 'M2MClientIdV2Export' <<<"$handoff_export" \
    || fail "federated export removed the replacement client ID"
grep -q 'M2MClientSecretNameV2Export' <<<"$handoff_export" \
    || fail "federated export removed the replacement secret-name reference"
grep -q 'workshop-outputs-\*\.json' "$SCRIPT_DIR/../.gitignore" \
    || fail "account-specific workshop exports are not gitignored"
echo "PASS: summaries/exports exclude unknown outputs and local exports are ignored"

echo "OK: all deploy-config checks passed"
