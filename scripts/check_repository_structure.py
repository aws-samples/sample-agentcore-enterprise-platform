"""Validate repository capability roots and dependency boundaries."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_TOP_LEVEL_DIRECTORIES = {
    ".agentcore",
    ".github",
    "agent-code",
    "control-library",
    "dashboard",
    "docker",
    "docs",
    "infra_utils",
    "migration",
    "presets",
    "scripts",
    "stacks",
    "tests",
    "tools",
    "use-cases",
}

LEGACY_CAPABILITY_ROOTS = {
    "migration-adapter",
    "migration-network-probe",
    "terraform",
    "workshop-simulation",
}

FORBIDDEN_IMPORTS = {
    "agent-code": {"infra_utils", "stacks"},
    "infra_utils": {"stacks"},
    "migration": {"infra_utils", "stacks"},
    "use-cases": {"stacks"},
}

REQUIRED_COMPATIBILITY_LINKS = {
    "terraform": "control-library/terraform",
}

AGENT_SHARED_DIRECTORIES = {"shared"}
AGENT_REQUIRED_FILES = {"Dockerfile", "agent.py", "requirements.txt"}


def repository_paths(repo_root: Path = REPO_ROOT) -> list[PurePosixPath]:
    """Return committed and new, non-ignored files that currently exist."""

    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return sorted(
        PurePosixPath(raw.decode())
        for raw in result.stdout.split(b"\0")
        if raw and (repo_root / raw.decode()).is_file()
    )


def check_top_level_directories(paths: list[PurePosixPath]) -> list[str]:
    roots = {path.parts[0] for path in paths if len(path.parts) > 1}
    errors = [
        f"unregistered top-level directory: {root}"
        for root in sorted(
            roots - ALLOWED_TOP_LEVEL_DIRECTORIES - LEGACY_CAPABILITY_ROOTS
        )
    ]
    errors.extend(
        f"legacy capability root must not return: {root}"
        for root in sorted(roots & LEGACY_CAPABILITY_ROOTS)
    )
    return errors


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def check_dependency_boundaries(
    paths: list[PurePosixPath], repo_root: Path = REPO_ROOT
) -> list[str]:
    errors: list[str] = []
    for path in paths:
        capability = path.parts[0]
        forbidden = FORBIDDEN_IMPORTS.get(capability)
        if not forbidden or path.suffix != ".py":
            continue
        try:
            tree = ast.parse((repo_root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            errors.append(f"{path}: cannot validate Python imports: {exc}")
            continue
        violations = _import_roots(tree) & forbidden
        if violations:
            errors.append(
                f"{path}: imports forbidden package(s): {', '.join(sorted(violations))}"
            )
    return errors


def check_agent_runtime_shape(paths: list[PurePosixPath]) -> list[str]:
    path_names = {str(path) for path in paths}
    runtime_directories = {
        path.parts[1]
        for path in paths
        if len(path.parts) > 2
        and path.parts[0] == "agent-code"
        and path.parts[1] not in AGENT_SHARED_DIRECTORIES
    }
    errors: list[str] = []
    for directory in sorted(runtime_directories):
        missing = {
            required
            for required in AGENT_REQUIRED_FILES
            if f"agent-code/{directory}/{required}" not in path_names
        }
        if missing:
            errors.append(
                f"agent-code/{directory}: missing runtime contract file(s): "
                f"{', '.join(sorted(missing))}"
            )
    return errors


def check_compatibility_links(repo_root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    for link_name, expected_target in REQUIRED_COMPATIBILITY_LINKS.items():
        link = repo_root / link_name
        if not link.is_symlink() or link.readlink() != Path(expected_target):
            errors.append(
                f"{link_name}: required compatibility link must target "
                f"{expected_target}"
            )
    return errors


def run_checks(repo_root: Path = REPO_ROOT) -> list[str]:
    paths = repository_paths(repo_root)
    return (
        check_top_level_directories(paths)
        + check_dependency_boundaries(paths, repo_root)
        + check_agent_runtime_shape(paths)
        + check_compatibility_links(repo_root)
    )


def main() -> int:
    try:
        errors = run_checks()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Repository structure check could not run: {exc}", file=sys.stderr)
        return 1

    if errors:
        print("Repository structure checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Repository structure checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
