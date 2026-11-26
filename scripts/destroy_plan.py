"""Destroy order for a synthesized CDK app, one line per dependency level.

A stack can only be deleted once every stack that depends on it is gone, so
the first level holds the stacks nothing remaining depends on; deploy.sh
deletes each level's stacks in parallel, then moves to the next.

Usage: destroy_plan.py MANIFEST [STACK ...]

MANIFEST is cdk.out/manifest.json. With STACK names, only those stacks are
planned (dependencies outside the set are ignored).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

STACK_ARTIFACT = "aws:cloudformation:stack"
RUNTIME_LOG_ROOT = "/aws/bedrock-agentcore/runtimes/"


def read_manifest(path: str | Path) -> dict[str, set[str]]:
    """Map each stack artifact in a cloud assembly manifest to its stack deps."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    artifacts = data.get("artifacts", {})
    stacks = {
        name for name, art in artifacts.items() if art.get("type") == STACK_ARTIFACT
    }
    return {
        name: {d for d in artifacts[name].get("dependencies", []) if d in stacks}
        for name in stacks
    }


def destroy_levels(deps: dict[str, set[str]]) -> list[list[str]]:
    """Group stacks into destroy levels; dependents come before dependencies.

    Dependencies on names outside ``deps`` are ignored. Raises ValueError on a
    dependency cycle.
    """
    names = set(deps)
    dependents: dict[str, set[str]] = {name: set() for name in names}
    for name, needs in deps.items():
        for dep in needs:
            if dep in names:
                dependents[dep].add(name)
    remaining = set(names)
    levels: list[list[str]] = []
    while remaining:
        level = sorted(n for n in remaining if not dependents[n] & remaining)
        if not level:
            raise ValueError("dependency cycle among: " + ", ".join(sorted(remaining)))
        levels.append(level)
        remaining.difference_update(level)
    return levels


def runtime_log_prefix(project: str, environment: str) -> str:
    """Log-group prefix of this deployment's AgentCore runtimes.

    Runtime names use '_' where the stack prefix uses '-'; the trailing '_'
    keeps 'demo' from matching 'demo2'. The prefix alone is still ambiguous
    ('demo_dev_' also prefixes project 'demo-dev', env 'dev'), so deletion
    uses is_deployment_runtime_log_group with the recorded runtime ids.
    """
    return RUNTIME_LOG_ROOT + f"{project}-{environment}".replace("-", "_") + "_"


def matches_runtime_log_group(name: str, prefix: str) -> bool:
    """True when the log group belongs to the deployment with this prefix."""
    return name.startswith(prefix)


def is_deployment_runtime_log_group(
    name: str, prefix: str, runtime_ids: list[str] | set[str]
) -> bool:
    """True only for log groups of runtimes recorded from this deployment.

    Each runtime id (or ARN) must itself sit under ``prefix``, and the log
    group must be ``<root><runtime-id>-...``. A runtime of another deployment
    whose name merely shares the prefix never matches.
    """
    for runtime_id in runtime_ids:
        base = RUNTIME_LOG_ROOT + runtime_id.rsplit("/", 1)[-1]
        if base.startswith(prefix) and name.startswith(base + "-"):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print CDK destroy levels.")
    parser.add_argument("manifest", help="path to cdk.out/manifest.json")
    parser.add_argument("stacks", nargs="*", help="restrict to these stacks")
    args = parser.parse_args(argv)
    try:
        deps = read_manifest(args.manifest)
        if args.stacks:
            unknown = sorted(set(args.stacks) - set(deps))
            if unknown:
                raise ValueError("not in the manifest: " + ", ".join(unknown))
            deps = {name: deps[name] for name in args.stacks}
        levels = destroy_levels(deps)
    except (OSError, ValueError) as exc:
        print(f"destroy_plan: {exc}", file=sys.stderr)
        return 1
    for level in levels:
        print(" ".join(level))
    return 0


if __name__ == "__main__":
    sys.exit(main())
