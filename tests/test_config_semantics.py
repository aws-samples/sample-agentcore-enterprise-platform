"""Cedar ENFORCE and gateway.tools semantics: no silent no-op settings."""

import tempfile
from pathlib import Path

from infra_utils.platform_config import (
    CEDAR_PERMIT_CONTROL,
    cedar_findings,
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
