#!/usr/bin/env bash
# Refuse to deploy when the CDK CLI cannot read the cloud assembly this app produces.
#
# aws-cdk-lib writes a cloud-assembly manifest at a schema version; the CDK CLI supports
# schema versions up to its own maximum. When the library is newer than the CLI, every cdk
# command dies with "Cloud assembly schema version mismatch" -- which reads like a broken app,
# not a stale CLI. This has cost us two debugging sessions. Run it before diff or deploy, on a
# workstation and in CI alike.
#
# Usage: ./preflight.sh [extra cdk context flags...]
#   ./preflight.sh -c coordinator_role=robcata
# Writes the synthesised assembly to the directory named by $CDK_OUT (default ./cdk.out) so a
# caller can reuse it with `cdk diff -a "$CDK_OUT"`.
set -euo pipefail

cd "$(dirname "$0")"
CDK_OUT="${CDK_OUT:-cdk.out}"
PY="${PY:-python3}"

fail() {
	echo "::error::CDK_MANIFEST_SCHEMA_MISMATCH: $1" >&2
	exit 1
}

command -v cdk >/dev/null 2>&1 || fail "no cdk CLI on PATH"
cli_version="$(cdk --version 2>/dev/null | awk '{print $1}')"
lib_version="$($PY -c 'import importlib.metadata as m; print(m.version("aws-cdk-lib"))' 2>/dev/null || echo unknown)"
echo "cdk CLI       $cli_version"
echo "aws-cdk-lib   $lib_version"

# Synthesise. This is also the gate: the CLI parses the assembly it just produced, so a schema
# the CLI cannot read fails right here with the library's own message.
synth_log="$(mktemp)"
trap 'rm -f "$synth_log"' EXIT
if ! cdk synth --quiet -o "$CDK_OUT" "$@" >"$synth_log" 2>&1; then
	cat "$synth_log" >&2
	if grep -qi "schema version" "$synth_log"; then
		fail "aws-cdk-lib $lib_version writes an assembly the cdk CLI $cli_version cannot read. Upgrade the CLI (npm i -g aws-cdk@latest) or pin aws-cdk-lib back."
	fi
	echo "::error::cdk synth failed for a reason other than the schema version; see above" >&2
	exit 1
fi

[ -f "$CDK_OUT/manifest.json" ] || fail "cdk synth wrote no $CDK_OUT/manifest.json"
schema="$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$CDK_OUT/manifest.json")"
echo "assembly schema $schema"

# Re-read the assembly through the CLI without re-running the app. `cdk ls -a` does nothing but
# load the manifest, so it fails if and only if the CLI cannot read this schema version.
if ! ls_log="$(cdk ls -a "$CDK_OUT" 2>&1)"; then
	echo "$ls_log" >&2
	fail "cdk CLI $cli_version cannot read assembly schema $schema written by aws-cdk-lib $lib_version"
fi

echo "$ls_log"
echo "preflight ok: cdk CLI $cli_version reads assembly schema $schema"
