"""Destroy matching rules, dependency batching, and the build waiter."""

import importlib.util
from pathlib import Path

import pytest

from infra_utils import build_trigger_lambda as btl

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "scripts_utils_destroy", _ROOT / "scripts" / "utils.py"
)
utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(utils)


def test_prefix_does_not_match_other_project():
    assert not utils.stack_belongs_to_deployment("demo2-dev-auth", "demo-dev")
    assert not utils.stack_belongs_to_deployment("demo-dev2-auth", "demo-dev")
    assert not utils.stack_belongs_to_deployment("demo-dev", "demo-dev")
    assert utils.stack_belongs_to_deployment("demo-dev-auth", "demo-dev")


def test_expected_names_match_exactly():
    expected = ["custom-stack"]
    assert utils.stack_belongs_to_deployment("custom-stack", "demo-dev", expected)
    assert not utils.stack_belongs_to_deployment("custom-stack2", "demo-dev", expected)


def test_runtime_log_group_exact_prefix_underscore():
    base = "/aws/bedrock-agentcore/runtimes/"
    assert utils.runtime_log_group_matches(
        base + "demo_dev_orchestrator-abc-DEFAULT", "demo-dev"
    )
    assert not utils.runtime_log_group_matches(
        base + "demo_dev2_orchestrator-abc-DEFAULT", "demo-dev"
    )
    assert not utils.runtime_log_group_matches(
        base + "demo2_dev_orchestrator-abc-DEFAULT", "demo-dev"
    )
    assert not utils.runtime_log_group_matches(
        "/aws/lambda/demo_dev_orchestrator", "demo-dev"
    )


def test_levels_independent_stacks_together():
    assert utils.destroy_levels(["a", "b", "c"], {}) == [["a", "b", "c"]]


def test_levels_chain():
    deps = {"c": ["b"], "b": ["a"]}
    assert utils.destroy_levels(["a", "b", "c"], deps) == [["c"], ["b"], ["a"]]


def test_levels_diamond():
    deps = {"obs": ["rt1", "rt2"], "rt1": ["auth"], "rt2": ["auth"]}
    levels = utils.destroy_levels(["auth", "rt1", "rt2", "obs"], deps)
    assert levels == [["obs"], ["rt1", "rt2"], ["auth"]]


def test_levels_ignore_deps_outside_set():
    assert utils.destroy_levels(["b"], {"b": ["gone"]}) == [["b"]]


def test_levels_cycle_raises():
    with pytest.raises(ValueError):
        utils.destroy_levels(["a", "b"], {"a": ["b"], "b": ["a"]})


class _FakeCodeBuild:
    def __init__(self, status):
        self.status = status

    def start_build(self, projectName, environmentVariablesOverride):
        return {"build": {"id": f"{projectName}:1"}}

    def batch_get_builds(self, ids):
        return {"builds": [{"buildStatus": self.status}]}


def _patch(monkeypatch, status):
    monkeypatch.setattr(btl, "_client", lambda: _FakeCodeBuild(status))


def test_on_event_returns_build_id(monkeypatch):
    _patch(monkeypatch, "IN_PROGRESS")
    event = {"RequestType": "Create", "ResourceProperties": {"ProjectName": "p"}}
    assert btl.on_event(event, None) == {"PhysicalResourceId": "p:1"}


def test_is_complete_polls(monkeypatch):
    event = {"RequestType": "Create", "PhysicalResourceId": "p:1"}
    _patch(monkeypatch, "IN_PROGRESS")
    assert btl.is_complete(event, None) == {"IsComplete": False}
    _patch(monkeypatch, "SUCCEEDED")
    assert btl.is_complete(event, None)["IsComplete"] is True
    _patch(monkeypatch, "FAILED")
    with pytest.raises(RuntimeError):
        btl.is_complete(event, None)


def test_delete_is_noop(monkeypatch):
    _patch(monkeypatch, "FAILED")
    event = {"RequestType": "Delete", "PhysicalResourceId": "p:1"}
    assert btl.on_event(event, None) == {"PhysicalResourceId": "p:1"}
    assert btl.is_complete(event, None) == {"IsComplete": True}


def test_waiter_covers_codebuild_timeout():
    from stacks import runtime_stack

    assert (
        runtime_stack.BUILD_WAIT_TIMEOUT_MINUTES > runtime_stack.BUILD_TIMEOUT_MINUTES
    )


def test_retention_for_days_rounds_up():
    from aws_cdk import aws_logs as logs

    from stacks.runtime_stack import retention_for_days

    assert retention_for_days(30) == logs.RetentionDays.ONE_MONTH
    assert retention_for_days(31) == logs.RetentionDays.TWO_MONTHS
    assert retention_for_days(1) == logs.RetentionDays.ONE_DAY
