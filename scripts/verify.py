#!/usr/bin/env python3
"""verify — one configuration-aware health check for the whole deployment.

Replaces scripts/test.py, which could not fail: it swallowed invoke
exceptions as "expected", hardcoded the default project name, and had no
non-zero exit path — a broken deployment ended with "Done." and exit 0.

This command asks the deployment contract (expected_stacks) what THIS
configuration promises, runs the matching existing tools, and exits non-zero
if any claim fails:

    stack in footprint          check
    ----------------------      ------------------------------------------
    gateway                     test_gateway.py       (MCP tools/list + call)
    memory                      test_memory.py        (event write/read)
    observability               check_observability.py
    networking                  check_network.py      (runtimes really in VPC;
                                                       not on a federated
                                                       platform side)
    (require_guardrails flag)   check_guardrail_enforcement.py (IAM simulation)
    (observability.alarms flag) check_alarms.py       (alarms exist, none firing)
    runtime-orchestrator        invoke.py             (live invoke; migration
                                                       uses a basic contract
                                                       check, normal agents use
                                                       pattern-specific checks)
    runtime-code-agent          invoke.py --a2a code-agent
    runtime-research-agent      invoke.py --a2a research-agent
    uc-<name> (enabled use case) use-cases/<name>/verify.py, run LAST so a
                                broken platform fails on the platform check

Configuration comes from platform.yaml when present (env vars win, same
precedence as everywhere else). Run it directly or via `deploy.sh verify`.
"""

import os
import subprocess  # nosec B404 — composing our own scripts, no shell
import sys
from collections.abc import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.platform_config import (
    PlatformConfig,
    discover_use_cases,
    load_platform_config,
    resolve_region,
    to_env,
)

HEALTH_PROMPT = "Health check: reply with one word."
CODE_INTERPRETER_MARKER = "CODE_INTERPRETER_OK"
# The Code Interpreter tool each tool-consuming pattern must call successfully.
# The Claude SDK patterns run it through their in-process code_int MCP server
# (mcp__codeint__execute_code); the others reach it through the gateway.
DEFAULT_REQUIRED_TOOL = "execute_python_securely"
REQUIRED_TOOLS = {
    "strands-agent": DEFAULT_REQUIRED_TOOL,
    "langgraph-agent": DEFAULT_REQUIRED_TOOL,
    "claude-sdk-agent": "execute_code",
    "claude-sdk-multi-agent": "execute_code",
    "agui-strands-agent": DEFAULT_REQUIRED_TOOL,
    "agui-langgraph-agent": DEFAULT_REQUIRED_TOOL,
}


def code_interpreter_prompt(tool: str) -> str:
    """The prompt that makes a pattern run the marker through its tool."""
    return (
        f"Use {tool} to run print('{CODE_INTERPRETER_MARKER}'). "
        "After the tool succeeds, reply with one word."
    )


def load_config() -> PlatformConfig:
    """platform.yaml when present, schema defaults otherwise; either way the
    child tools see the same values through the environment (env wins)."""
    path = Path(os.environ.get("PLATFORM_CONFIG", REPO / "platform.yaml"))
    config = load_platform_config(path) if path.exists() else PlatformConfig()
    # Without a manifest, to_env() would pin the schema default (us-east-1)
    # into the child tools' environment even when workshop.env or the AWS
    # profile says otherwise — resolve the region for real first.
    os.environ.setdefault("AWS_REGION", resolve_region())
    for key, value in to_env(config).items():
        os.environ.setdefault(key, value)
    return config


def checks_for(
    suffixes: set[str],
    agent_pattern: str,
    require_guardrails: bool = False,
    alarms: bool = False,
    use_cases: Sequence[str] = (),
    migration: bool = False,
    role: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Map a footprint onto the tools that verify it. Pure — unit-tested.

    `role` is the federated side (PlatformConfig.federated_role). The platform
    side runs no agents, so only platform resources (identity, gateway with its
    policies, observability, alarms, use cases) are checked there; memory,
    networking, guardrail-enforcement and runtime checks are skipped.

    `use_cases` are the names of enabled use cases; each contributes its own
    verify.py (CONTRIBUTING_USE_CASES.md), appended after the core checks.
    Paths are relative to scripts/, which is where main() runs every tool."""
    checks: list[tuple[str, list[str]]] = []
    agents_here = role != "platform"
    if "auth" in suffixes:
        # First, deliberately: every later check mints a token through the same
        # issuer, and this one turns an opaque 401 into the IdP-side fix.
        checks.append(("identity", ["check_identity.py"]))
    if "gateway" in suffixes:
        checks.append(("gateway", ["test_gateway.py"]))
    if "memory" in suffixes and agents_here:
        checks.append(("memory", ["test_memory.py"]))
    if "observability" in suffixes:
        checks.append(("observability", ["check_observability.py"]))
    if "networking" in suffixes and agents_here:
        checks.append(("networking", ["check_network.py"]))
    if require_guardrails and agents_here:
        checks.append(("guardrail enforcement", ["check_guardrail_enforcement.py"]))
    if alarms:
        checks.append(("alarms", ["check_alarms.py"]))
    if "runtime-orchestrator" in suffixes and agents_here:
        if migration:
            # A migrated image comes from migration.source, not agents.pattern.
            # Prove its AgentCore HTTP contract with a real invoke, without
            # assuming that the customer image exposes our Code Interpreter
            # tool or speaks AG-UI.
            invoke_args = ["invoke.py", "--require-success", HEALTH_PROMPT]
        else:
            agui = ["--agui"] if agent_pattern.startswith("agui-") else []
            # The pattern picks the stream parser for the tool-use check.
            pattern_arg = ["--pattern", agent_pattern]
            if agent_pattern == "orchestrator":
                # The orchestrator reports a failed run as HTTP 200 with
                # {"status": "error"}, so transport success is not enough.
                invoke_args = [
                    "invoke.py",
                    *agui,
                    *pattern_arg,
                    "--require-success",
                    HEALTH_PROMPT,
                ]
            else:
                tool = REQUIRED_TOOLS.get(agent_pattern, DEFAULT_REQUIRED_TOOL)
                invoke_args = [
                    "invoke.py",
                    *agui,
                    *pattern_arg,
                    "--require-tool",
                    tool,
                    "--require-tool-result",
                    CODE_INTERPRETER_MARKER,
                    code_interpreter_prompt(tool),
                ]
        checks.append(("orchestrator invoke", invoke_args))
    if "runtime-code-agent" in suffixes and agents_here:
        checks.append(
            ("a2a code-agent", ["invoke.py", "--a2a", "code-agent", HEALTH_PROMPT])
        )
    if "runtime-research-agent" in suffixes and agents_here:
        checks.append(
            (
                "a2a research-agent",
                ["invoke.py", "--a2a", "research-agent", HEALTH_PROMPT],
            )
        )
    for name in use_cases:
        checks.append((f"use case {name}", [f"../use-cases/{name}/verify.py"]))
    return checks


def run_checks(checks: list[tuple[str, list[str]]], runner=subprocess.run) -> list[str]:
    """Run each check in order and return the names that failed.

    Headers are flushed before each child starts, and children run
    unbuffered, so stage output never interleaves out of order."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    failed: list[str] = []
    for name, argv in checks:
        print(f"── {name} " + "─" * max(1, 58 - len(name)), flush=True)
        cmd = [sys.executable, str(REPO / "scripts" / argv[0]), *argv[1:]]
        result = runner(cmd, cwd=REPO / "scripts", check=False, env=env)  # nosec B603
        if result.returncode != 0:
            failed.append(name)
        print(flush=True)
    return failed


def main() -> int:
    config = load_config()

    # The footprint is role-dependent in a federation; the account decides.
    import boto3

    account = boto3.client("sts").get_caller_identity()["Account"]
    prefix = f"{config.project}-{config.environment}-"
    suffixes = {s.removeprefix(prefix) for s in config.expected_stacks(account)}
    role = config.federated_role(account)
    pattern = os.environ.get("AGENT_PATTERN", config.agents.pattern)
    require_guardrails = (
        os.environ.get(
            "REQUIRE_GUARDRAILS", str(config.security.require_guardrails).lower()
        )
        == "true"
    )
    alarms = (
        os.environ.get("ENABLE_ALARMS", str(config.observability.alarms).lower())
        == "true"
    )

    # check_identity.py compares the deployed issuer mode against the design.
    os.environ.setdefault("IDP_MODE", config.identity.mode)
    # Enabled use cases whose stacks are actually in this footprint (in a
    # federation, expected_stacks already placed them on the right side).
    manifests = discover_use_cases() if config.use_cases else {}
    use_cases = [
        name
        for name in sorted(config.use_cases)
        if any(s in suffixes for s in manifests[name].stacks)
    ]
    checks = checks_for(
        suffixes,
        pattern,
        require_guardrails,
        alarms,
        use_cases,
        migration=config.migration is not None,
        role=role,
    )
    print(
        f"Verifying {config.project}/{config.environment} in account {account}",
        flush=True,
    )
    print(f"Footprint: {' '.join(sorted(suffixes))}\n", flush=True)

    failed = run_checks(checks)

    print("═" * 60, flush=True)
    if failed:
        print(f"FAILED ({len(failed)}/{len(checks)}): {', '.join(failed)}", flush=True)
        return 1
    print(f"OK: all {len(checks)} checks passed for this footprint", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
