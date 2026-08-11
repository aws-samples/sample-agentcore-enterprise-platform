"""AG-UI patterns need the AGUI protocol, and must stay authenticated.

Regression guard: the protocol used to come from runtime_type alone, so an
agui-* pattern deployed as plain HTTP and its typed SSE events were proxied
unchanged (found while running the live pattern matrix). The authorizer check
matters just as much — widening the protocol list without it would deploy a
runtime with no inbound authorizer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra_utils.runtime_protocol import (
    PROTOCOLS,
    needs_jwt_authorizer,
    resolve_protocol,
)

AGENT_CODE = Path(__file__).resolve().parents[1] / "agent-code"


def test_agui_patterns_get_the_agui_protocol():
    for pattern in ("agui-strands-agent", "agui-langgraph-agent"):
        assert resolve_protocol("orchestrator", pattern) == "AGUI"


def test_every_agui_pattern_on_disk_is_covered():
    """A new agui-* directory must not silently deploy as HTTP."""
    patterns = [p.name for p in AGENT_CODE.iterdir() if p.name.startswith("agui-")]
    assert patterns, "expected agui-* patterns in agent-code/"
    for pattern in patterns:
        assert resolve_protocol("orchestrator", pattern) == "AGUI", pattern


def test_non_agui_patterns_are_unchanged():
    for pattern in ("", "orchestrator", "strands-agent", "claude-sdk-multi-agent"):
        assert resolve_protocol("orchestrator", pattern) == "HTTP"
    assert resolve_protocol("a2a_agent", "strands-agent") == "A2A"
    assert resolve_protocol("mcp_server") == "MCP"


def test_pattern_never_overrides_a_non_http_runtime_type():
    """An A2A or MCP runtime keeps its protocol whatever the pattern is."""
    assert resolve_protocol("a2a_agent", "agui-strands-agent") == "A2A"
    assert resolve_protocol("mcp_server", "agui-strands-agent") == "MCP"


def test_client_facing_protocols_are_authenticated():
    for protocol in ("HTTP", "MCP", "AGUI"):
        assert needs_jwt_authorizer(protocol), f"{protocol} must carry an authorizer"
    assert not needs_jwt_authorizer("A2A")


def test_resolved_protocols_are_valid_cfn_values():
    for runtime_type in ("orchestrator", "a2a_agent", "mcp_server", "unknown"):
        assert resolve_protocol(runtime_type, "agui-strands-agent") in PROTOCOLS
