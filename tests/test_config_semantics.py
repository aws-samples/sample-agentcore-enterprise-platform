"""Cedar ENFORCE and gateway.tools semantics: no silent no-op settings."""

import tempfile
from pathlib import Path

from infra_utils import platform_config
from infra_utils.platform_config import (
    CEDAR_PERMIT_CONTROL,
    CEDAR_PERMIT_CONTROL_ID,
    CEDAR_POLICY_CONTROLS,
    cedar_findings,
    cedar_permitted_tools,
    gateway_tool_errors,
    known_gateway_tools,
)

REPO = Path(__file__).resolve().parents[1]


def test_enforce_with_engine_disabled_is_an_error():
    errors, warnings = cedar_findings(
        enabled=False, mode="ENFORCE", tool_names=["sample-tool"], production=False
    )
    assert errors and "cedar.enabled" in errors[0]
    assert warnings == []


def test_enforce_with_uncovered_tool_refused_in_production():
    errors, _ = cedar_findings(
        enabled=True,
        mode="ENFORCE",
        tool_names=["sample-tool", "web-search"],
        production=True,
    )
    assert len(errors) == 1
    assert "web-search" in errors[0]
    assert CEDAR_PERMIT_CONTROL in errors[0]


def test_enforce_with_uncovered_tool_warns_in_workshop():
    errors, warnings = cedar_findings(
        enabled=True, mode="ENFORCE", tool_names=["web-search"], production=False
    )
    assert errors == []
    assert warnings and CEDAR_PERMIT_CONTROL in warnings[0]


def test_enforce_with_covered_tools_and_log_only_pass():
    assert cedar_findings(
        enabled=True, mode="ENFORCE", tool_names=["sample-tool"], production=True
    ) == ([], [])
    assert cedar_findings(
        enabled=True, mode="LOG_ONLY", tool_names=["web-search"], production=True
    ) == ([], [])


def test_coverage_is_read_from_the_deployed_permit():
    covered = cedar_permitted_tools()
    assert covered is None or "sample-tool" in covered


def test_custom_permit_covers_custom_tool():
    policy = (
        "permit(principal, action in ["
        'AgentCore::Action::"sample-tool___text_analysis_tool", '
        'AgentCore::Action::"crm___lookup"], resource);'
    )
    covered = cedar_permitted_tools(policy)
    assert covered == frozenset({"sample-tool", "crm"})
    assert cedar_findings(
        enabled=True,
        mode="ENFORCE",
        tool_names=["sample-tool", "crm"],
        production=True,
        permitted_tools=covered,
    ) == ([], [])


def test_unconstrained_action_permit_covers_everything():
    assert cedar_permitted_tools("permit(principal, action, resource);") is None
    assert cedar_findings(
        enabled=True,
        mode="ENFORCE",
        tool_names=["web-search"],
        production=True,
        permitted_tools=None,
    ) == ([], [])


def test_uncovered_message_names_the_deployed_permit():
    errors, _ = cedar_findings(
        enabled=True,
        mode="ENFORCE",
        tool_names=["crm"],
        production=True,
        permitted_tools=frozenset({"sample-tool"}),
    )
    assert errors and CEDAR_PERMIT_CONTROL_ID in errors[0]
    assert "crm" in errors[0]


def test_uncovered_message_names_every_step():
    errors, _ = cedar_findings(
        enabled=True,
        mode="ENFORCE",
        tool_names=["crm"],
        production=True,
        permitted_tools=frozenset({"sample-tool"}),
    )
    message = errors[0]
    for step in (
        'AgentCore::Action::"<target>___<tool>"',
        CEDAR_PERMIT_CONTROL_ID,
        CEDAR_PERMIT_CONTROL,
        "control-library/cedar/",
        "CEDAR_POLICY_CONTROLS",
        "infra_utils/platform_config.py",
        "stacks/gateway_stack.py",
        "LOG_ONLY",
    ):
        assert step in message, step


def test_coverage_reads_every_registered_control(monkeypatch):
    from infra_utils import policy_loader

    texts = {
        "a": 'permit(principal, action in [AgentCore::Action::"sample-tool___t"], resource);',
        "b": 'permit(principal, action in [AgentCore::Action::"crm___lookup"], resource);',
    }
    monkeypatch.setattr(platform_config, "CEDAR_POLICY_CONTROLS", {"A": "a", "B": "b"})
    monkeypatch.setattr(
        policy_loader, "load_control_text", lambda control_id, _vars: texts[control_id]
    )
    assert cedar_permitted_tools() == frozenset({"sample-tool", "crm"})


def test_gateway_stack_deploys_the_registered_controls():
    src = (REPO / "stacks" / "gateway_stack.py").read_text()
    assert "from infra_utils.platform_config import CEDAR_POLICY_CONTROLS" in src
    assert "CEDAR_POLICY_CONTROLS.items()" in src
    assert "cedar_controls = {" not in src
    assert CEDAR_POLICY_CONTROLS["PermitReadTools"] == CEDAR_PERMIT_CONTROL_ID


def test_empty_tools_is_an_error():
    assert gateway_tool_errors([])


def test_unknown_tool_names_are_listed_with_the_valid_ones():
    errors = gateway_tool_errors(["sample-tool", "nope"], known=["sample-tool"])
    assert len(errors) == 1
    assert "nope" in errors[0] and "sample-tool" in errors[0]
    assert gateway_tool_errors(["sample-tool"], known=["sample-tool"]) == []


def test_blocks_are_not_checked_as_bare_names():
    assert gateway_tool_errors([{"name": "x", "type": "openapi"}], known=[]) == []


def test_known_tools_come_from_tool_specs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "my_tool").mkdir()
        (root / "my_tool" / "tool_spec.json").write_text("[]")
        (root / "no_spec").mkdir()
        assert known_gateway_tools(root) == ["my-tool", "sample-tool"]
    assert "sample-tool" in known_gateway_tools()


def test_app_py_runs_the_checks():
    src = (REPO / "app.py").read_text()
    assert "gateway_tool_errors(" in src
    assert "cedar_findings(" in src
