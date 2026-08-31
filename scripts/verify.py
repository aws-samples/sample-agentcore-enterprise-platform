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
    networking                  check_network.py      (runtimes really in VPC)
    (require_guardrails flag)   check_guardrail_enforcement.py (IAM simulation)
    runtime-orchestrator        invoke.py             (live invoke; --agui for
                                                       agui-* agent patterns)
    runtime-code-agent          invoke.py --a2a code-agent
    runtime-research-agent      invoke.py --a2a research-agent

Configuration comes from platform.yaml when present (env vars win, same
precedence as everywhere else). Run it directly or via `deploy.sh verify`.
"""

import os
import subprocess  # nosec B404 — composing our own scripts, no shell
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.platform_config import (
    PlatformConfig,
    load_platform_config,
    resolve_region,
    to_env,
)

HEALTH_PROMPT = "Health check: reply with one word."


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
    suffixes: set[str], agent_pattern: str, require_guardrails: bool = False
) -> list[tuple[str, list[str]]]:
    """Map a footprint onto the tools that verify it. Pure — unit-tested."""
    checks: list[tuple[str, list[str]]] = []
    if "gateway" in suffixes:
        checks.append(("gateway", ["test_gateway.py"]))
    if "memory" in suffixes:
        checks.append(("memory", ["test_memory.py"]))
    if "observability" in suffixes:
        checks.append(("observability", ["check_observability.py"]))
    if "networking" in suffixes:
        checks.append(("networking", ["check_network.py"]))
    if require_guardrails:
        checks.append(("guardrail enforcement", ["check_guardrail_enforcement.py"]))
    if "runtime-orchestrator" in suffixes:
        agui = ["--agui"] if agent_pattern.startswith("agui-") else []
        checks.append(("orchestrator invoke", ["invoke.py", *agui, HEALTH_PROMPT]))
    if "runtime-code-agent" in suffixes:
        checks.append(
            ("a2a code-agent", ["invoke.py", "--a2a", "code-agent", HEALTH_PROMPT])
        )
    if "runtime-research-agent" in suffixes:
        checks.append(
            (
                "a2a research-agent",
                ["invoke.py", "--a2a", "research-agent", HEALTH_PROMPT],
            )
        )
    return checks


def main() -> int:
    config = load_config()

    # The footprint is role-dependent in a federation; the account decides.
    import boto3

    account = boto3.client("sts").get_caller_identity()["Account"]
    prefix = f"{config.project}-{config.environment}-"
    suffixes = {s.removeprefix(prefix) for s in config.expected_stacks(account)}
    pattern = os.environ.get("AGENT_PATTERN", config.agents.pattern)
    require_guardrails = (
        os.environ.get(
            "REQUIRE_GUARDRAILS", str(config.security.require_guardrails).lower()
        )
        == "true"
    )

    checks = checks_for(suffixes, pattern, require_guardrails)
    print(f"Verifying {config.project}/{config.environment} in account {account}")
    print(f"Footprint: {' '.join(sorted(suffixes))}\n")

    failed: list[str] = []
    for name, argv in checks:
        print(f"── {name} " + "─" * max(1, 58 - len(name)))
        cmd = [sys.executable, str(REPO / "scripts" / argv[0]), *argv[1:]]
        result = subprocess.run(cmd, cwd=REPO / "scripts", check=False)  # nosec B603
        if result.returncode != 0:
            failed.append(name)
        print()

    print("═" * 60)
    if failed:
        print(f"FAILED ({len(failed)}/{len(checks)}): {', '.join(failed)}")
        return 1
    print(f"OK: all {len(checks)} checks passed for this footprint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
