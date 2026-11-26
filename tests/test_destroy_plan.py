"""Tests for scripts/destroy_plan.py (destroy levels, runtime log matching)."""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "destroy_plan.py"
_SPEC = importlib.util.spec_from_file_location("destroy_plan", _PATH)
destroy_plan = importlib.util.module_from_spec(_SPEC)
sys.modules["destroy_plan"] = destroy_plan
_SPEC.loader.exec_module(destroy_plan)


def test_chain_destroys_dependents_first():
    deps = {"a": set(), "b": {"a"}, "c": {"b"}}
    assert destroy_plan.destroy_levels(deps) == [["c"], ["b"], ["a"]]


def test_diamond_groups_siblings():
    deps = {"a": set(), "b": {"a"}, "c": {"a"}, "d": {"b", "c"}}
    assert destroy_plan.destroy_levels(deps) == [["d"], ["b", "c"], ["a"]]


def test_independent_stacks_share_one_level():
    deps = {"z": set(), "x": set(), "y": set()}
    assert destroy_plan.destroy_levels(deps) == [["x", "y", "z"]]


def test_cycle_raises():
    with pytest.raises(ValueError, match="cycle"):
        destroy_plan.destroy_levels({"a": {"b"}, "b": {"a"}, "c": set()})


def test_dependencies_outside_the_set_are_ignored():
    deps = {"a": {"gone"}, "b": {"a", "other"}}
    assert destroy_plan.destroy_levels(deps) == [["b"], ["a"]]


def test_empty_set_has_no_levels():
    assert destroy_plan.destroy_levels({}) == []


@pytest.fixture
def workdir():
    # Own temp dir: independent of pytest's basetemp, which may not exist.
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _manifest(directory):
    data = {
        "version": "36.0.0",
        "artifacts": {
            "p-auth": {"type": "aws:cloudformation:stack"},
            "p-gateway": {
                "type": "aws:cloudformation:stack",
                "dependencies": ["p-auth", "p-gateway.assets"],
            },
            "p-gateway.assets": {"type": "cdk:asset-manifest"},
            "Tree": {"type": "cdk:tree"},
        },
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_read_manifest_keeps_only_stacks(workdir):
    deps = destroy_plan.read_manifest(_manifest(workdir))
    assert deps == {"p-auth": set(), "p-gateway": {"p-auth"}}


def test_cli_prints_one_line_per_level(workdir, capsys):
    assert destroy_plan.main([str(_manifest(workdir))]) == 0
    assert capsys.readouterr().out == "p-gateway\np-auth\n"


def test_cli_restricts_to_named_stacks(workdir, capsys):
    assert destroy_plan.main([str(_manifest(workdir)), "p-auth"]) == 0
    assert capsys.readouterr().out == "p-auth\n"


def test_cli_fails_on_missing_manifest(workdir, capsys):
    assert destroy_plan.main([str(workdir / "absent.json")]) == 1
    assert "destroy_plan:" in capsys.readouterr().err


def test_runtime_log_prefix_replaces_dashes():
    prefix = destroy_plan.runtime_log_prefix("lv-163", "ma")
    assert prefix == "/aws/bedrock-agentcore/runtimes/lv_163_ma_"
    assert destroy_plan.matches_runtime_log_group(
        "/aws/bedrock-agentcore/runtimes/lv_163_ma_lv_code_agent-AbC123", prefix
    )


def test_runtime_log_match_is_exact():
    prefix = destroy_plan.runtime_log_prefix("demo", "dev")
    root = "/aws/bedrock-agentcore/runtimes/"
    assert destroy_plan.matches_runtime_log_group(root + "demo_dev_agent-1", prefix)
    assert not destroy_plan.matches_runtime_log_group(
        root + "demo2_dev_agent-1", prefix
    )
    other = destroy_plan.runtime_log_prefix("demo", "dev2")
    assert not destroy_plan.matches_runtime_log_group(root + "demo_dev_x-1", other)
    assert not destroy_plan.matches_runtime_log_group(
        "/aws/lambda/demo_dev_agent", prefix
    )
