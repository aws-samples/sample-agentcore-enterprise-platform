#!/usr/bin/env bash
# Self-check for the guided workshop flow in deploy.sh (no AWS, no framework).
# (a) every module in every PROFILE_MODULES sequence has MAP/EXPLAIN/VERIFY entries
# (b) `workshop --dry-run --profile greenfield` prints 3 4 5 6 9 in order, no AWS call
# (c) `--from 6` starts at module 6   (d) unknown --profile errors with the valid list
set -euo pipefail
[ "${BASH_VERSINFO[0]:-0}" -ge 4 ] || { echo "needs bash 4+ (brew install bash)" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# (a) Extract only the module/profile tables out of deploy.sh (each is one physical line).
# shellcheck disable=SC2034  # used by the eval'd MODULE_MAP[...] lines below
PREFIX="agentcore-workshop-dev"
eval "$(sed -n '/^declare -A MODULE_MAP/p; /^MODULE_MAP\[/p;
                /^declare -A MODULE_EXPLAIN/p; /^MODULE_EXPLAIN\[/p;
                /^declare -A MODULE_VERIFY/p; /^MODULE_VERIFY\[/p;
                /^declare -A PROFILE_MODULES/p; /^PROFILE_MODULES\[/p' "$SCRIPT_DIR/deploy.sh")"
[ "${#PROFILE_MODULES[@]}" -eq 5 ] || fail "expected 5 profiles, got ${#PROFILE_MODULES[@]}"
for p in "${!PROFILE_MODULES[@]}"; do
    for m in ${PROFILE_MODULES[$p]}; do
        [ -n "${MODULE_MAP[$m]:-}" ]     || fail "profile $p: module $m has no MODULE_MAP entry"
        [ -n "${MODULE_EXPLAIN[$m]:-}" ] || fail "profile $p: module $m has no MODULE_EXPLAIN entry"
        [ -n "${MODULE_VERIFY[$m]:-}" ]  || fail "profile $p: module $m has no MODULE_VERIFY entry"
    done
done
echo "PASS: every sequenced module has MAP + EXPLAIN + VERIFY"

# Sandboxed live runs: copy deploy.sh; a fake `aws` on PATH records any call.
mkdir -p "$TMP/scripts" "$TMP/bin"
cp "$SCRIPT_DIR/deploy.sh" "$TMP/scripts/"
printf '#!/bin/sh\ntouch "%s/aws-was-called"\nexit 1\n' "$TMP" > "$TMP/bin/aws"
chmod +x "$TMP/bin/aws"
run() { PATH="$TMP/bin:$PATH" "$BASH" "$TMP/scripts/deploy.sh" "$@" 2>&1; }

# (b) greenfield dry run: modules in order, stacks + verify shown, zero AWS calls.
out="$(run workshop --dry-run --profile greenfield)"
mods="$(grep -o 'Module [0-9A-E] —' <<<"$out" | awk '{print $2}' | paste -sd' ' -)"
[ "$mods" = "3 4 5 6 9" ] || fail "greenfield module order: got '$mods'"
grep -q 'Would deploy: agentcore-workshop-dev-auth' <<<"$out" || fail "module 3 stacks not shown"
grep -q 'Would deploy: agentcore-workshop-dev-runtime-orchestrator' <<<"$out" || fail "module 6 stacks not shown"
grep -q 'Would verify: .venv/bin/python scripts/invoke.py' <<<"$out" || fail "module 6 verify not shown"
[ ! -f "$TMP/aws-was-called" ] || fail "dry run made an AWS call"
echo "PASS: greenfield dry run prints 3 4 5 6 9 in order with stacks + verify, no AWS"

# (c) --from 6 skips earlier modules.
out="$(run workshop --dry-run --profile greenfield --from 6)"
mods="$(grep -o 'Module [0-9A-E] —' <<<"$out" | awk '{print $2}' | paste -sd' ' -)"
[ "$mods" = "6 9" ] || fail "--from 6: got '$mods'"
echo "PASS: --from 6 starts at module 6"

# (d) unknown profile errors and names the valid ones.
if out="$(run workshop --dry-run --profile nope)"; then fail "unknown profile did not error"; fi
grep -q 'Valid profiles' <<<"$out" || fail "unknown profile error lacks the valid list"
echo "PASS: unknown --profile rejected with valid list"

echo "OK: all workshop-flow checks passed"
