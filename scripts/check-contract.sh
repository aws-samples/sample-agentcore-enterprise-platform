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
printf '{ "availability-zones:account=%s:region=%s": ["%sa","%sb"] }\n' \
    "$CDK_DEFAULT_ACCOUNT" "$CDK_DEFAULT_REGION" \
    "$CDK_DEFAULT_REGION" "$CDK_DEFAULT_REGION" > cdk.context.json

fail=0
for preset in presets/*.yaml; do
    name="$(basename "$preset" .yaml)"
    workdir="$(mktemp -d)"

    "$PY" -m infra_utils.platform_config --stacks "$preset" | sort > "$workdir/expected"

    if ! npx --no-install cdk ls \
        --app "env PLATFORM_CONFIG=$preset $PY app.py" \
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
