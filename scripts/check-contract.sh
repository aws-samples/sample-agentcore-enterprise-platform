#!/usr/bin/env bash
# Preset ↔ synth parity: the deployment contract must match what app.py builds.
#
# For every presets/*.yaml: ask the contract for its expected stack list
# (platform_config --stacks), synthesize the app with that preset, and fail on
# any difference. This is what keeps expected_stacks() honest — add a stack to
# app.py without teaching the contract (or vice versa) and this goes red.
#
# No AWS credentials needed: account is a placeholder and the networking VPC's
# AZ lookup is pre-seeded via cdk.context.json (docs/TESTING.md A7b).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO"

PY="${PY:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-111111111111}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-us-east-1}"
# The shipped presets carry placeholders (REPLACE_ME client ids, sentinel
# account ids) on purpose; a real deploy refuses them. The gate only proves the
# presets synthesize, so it downgrades those refusals to warnings.
export PLATFORM_ALLOW_PLACEHOLDERS=1
# The parity synth runs offline; AZ pinning needs credentials to read the account's AZ ids.
export AGENTCORE_SKIP_AZ_PINNING=1
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1

# The networking VPC's AZ lookup needs context to synth offline; CDK only
# reads it from ./cdk.context.json (docs/TESTING.md A7b). Preserve any
# existing file and put ours in place for the duration of the run.
[ -f cdk.context.json ] && mv cdk.context.json cdk.context.json.check-contract-bak
restore_context() {
    rm -f cdk.context.json
    # Plain `[ -f ] && mv` would end the trap with status 1 when no backup
    # exists (fresh checkout, CI) and set -e turns that into the script's
    # exit code — every preset PASSed and the job still failed. If/fi is the
    # always-zero form.
    if [ -f cdk.context.json.check-contract-bak ]; then
        mv cdk.context.json.check-contract-bak cdk.context.json
    fi
}
# PIPE included: `check-contract.sh | head` must not skip the restore.
trap restore_context EXIT INT TERM PIPE
# Presets are the shipped profiles; tests/fixtures/contract/ holds extra
# footprints worth pinning (e.g. a use case enabled), same parity rules.
CONFIGS=(presets/*.yaml)
[ -d tests/fixtures/contract ] && CONFIGS+=(tests/fixtures/contract/*.yaml)
# A federated preset is synthesized from BOTH of its accounts (below), so the
# AZ context covers every account id a preset names, not just the default.
preset_accounts() {
    "$PY" - "$@" <<'PYEOF'
import sys, yaml
seen = []
for f in sys.argv[1:]:
    dep = (yaml.safe_load(open(f)) or {}).get("deployment") or {}
    for a in [dep.get("platform_account", ""), *(dep.get("workload_accounts") or [])]:
        if a and a not in seen:
            seen.append(a)
print(" ".join(seen))
PYEOF
}
{
    echo '{'
    for acct in "$CDK_DEFAULT_ACCOUNT" $(preset_accounts "${CONFIGS[@]}"); do
        printf '  "availability-zones:account=%s:region=%s": ["%sa","%sb"],\n' \
            "$acct" "$CDK_DEFAULT_REGION" "$CDK_DEFAULT_REGION" "$CDK_DEFAULT_REGION"
    done
    echo '  "_": null'
    echo '}'
} > cdk.context.json

# The account you deploy into decides a federated footprint, so a federated
# preset is checked once per side; every other preset once, from the default.
sides_of() {
    "$PY" - "$1" <<'PYEOF'
import sys, yaml
dep = (yaml.safe_load(open(sys.argv[1])) or {}).get("deployment") or {}
if dep.get("strategy") == "federated":
    print(" ".join([dep.get("platform_account", ""), *(dep.get("workload_accounts") or [])]))
PYEOF
}
RUNS=()
for preset in "${CONFIGS[@]}"; do
    sides="$(sides_of "$preset")"
    if [ -n "$sides" ]; then
        for acct in $sides; do RUNS+=("$preset:$acct"); done
    else
        RUNS+=("$preset:$CDK_DEFAULT_ACCOUNT")
    fi
done

fail=0
# security-focused ships the obvious org placeholder o-xxxxxxxxxx; the synth
# below runs with it (downgraded to a warning), and a real deploy must refuse it.
if org_out=$(env -u PLATFORM_ALLOW_PLACEHOLDERS "$PY" -m infra_utils.platform_config presets/security-focused.yaml 2>&1); then
    echo "FAIL: security-focused validated with its placeholder org id" >&2
    fail=1
elif ! grep -q "security.org_id" <<< "$org_out"; then
    echo "FAIL: security-focused rejection does not name security.org_id" >&2
    fail=1
else
    echo "PASS: security-focused placeholder org id is refused for a real deploy"
fi
for run in "${RUNS[@]}"; do
    preset="${run%%:*}"; acct="${run##*:}"
    name="$(basename "$preset" .yaml)"
    [ "$acct" != "$CDK_DEFAULT_ACCOUNT" ] && name="$name@$acct"
    workdir="$(mktemp -d)"

    CDK_DEFAULT_ACCOUNT="$acct" "$PY" -m infra_utils.platform_config --stacks "$preset" 2>/dev/null | sort > "$workdir/expected"

    if ! CDK_DEFAULT_ACCOUNT="$acct" npx --no-install cdk ls \
        --app "env PLATFORM_CONFIG=$preset CDK_DEFAULT_ACCOUNT=$acct $PY app.py" \
        --output "$workdir/cdk.out" 2> "$workdir/synth.log" \
        | sort > "$workdir/actual"; then
        echo "FAIL: $name — synth broke:" >&2
        tail -5 "$workdir/synth.log" >&2
        fail=1
        rm -rf "$workdir"
        continue
    fi

    if ! diff -u "$workdir/expected" "$workdir/actual" > "$workdir/diff"; then
        echo "FAIL: $name — contract and synth disagree (expected vs synthesized):" >&2
        cat "$workdir/diff" >&2
        fail=1
    else
        printf 'PASS: %-18s %s stack(s)\n' "$name" "$(wc -l < "$workdir/expected" | tr -d ' ')"
    fi
    rm -rf "$workdir"
done

[ "$fail" = 0 ] && echo "OK: every preset matches the contract"
exit "$fail"
