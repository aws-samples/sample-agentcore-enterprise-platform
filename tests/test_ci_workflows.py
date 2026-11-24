"""Checks that the full unit-test suite runs on every PR and push to main."""

import re
from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "unit-tests.yml"
)
PINNED_USES = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def _load():
    assert WORKFLOW.is_file(), f"missing {WORKFLOW}"
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(data):
    # PyYAML parses a bare `on:` key as boolean True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _steps(data):
    steps = []
    for job in data["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def test_triggers_on_pull_request_and_push():
    triggers = _triggers(_load())
    assert "pull_request" in triggers
    assert "push" in triggers


def test_no_path_filters():
    triggers = _triggers(_load())
    for name in ("pull_request", "push"):
        cfg = triggers.get(name) or {}
        assert "paths" not in cfg, name
        assert "paths-ignore" not in cfg, name


def test_push_targets_main():
    push = _triggers(_load())["push"] or {}
    assert "main" in push.get("branches", [])


def test_actions_pinned_by_sha():
    uses = [s["uses"] for s in _steps(_load()) if "uses" in s]
    assert uses
    for ref in uses:
        assert PINNED_USES.match(ref), ref


def test_runs_full_pytest_suite():
    runs = [s.get("run", "") for s in _steps(_load())]
    assert any("python -m pytest tests/" in r for r in runs)
