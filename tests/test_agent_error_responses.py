"""Agents never return exception text to callers.

Runtime checks load the orchestrator and strands agents with their heavy
dependencies stubbed in sys.modules (restored by monkeypatch). A static check
covers every agent pattern.
"""

import asyncio
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENT_CODE = REPO / "agent-code"
AGENT_PATTERNS = (
    "orchestrator",
    "strands-agent",
    "langgraph-agent",
    "claude-sdk-agent",
    "claude-sdk-multi-agent",
    "agui-strands-agent",
    "agui-langgraph-agent",
)
SECRET = "db-password=hunter2"


class _App:
    def entrypoint(self, fn):
        return fn

    def run(self, *args, **kwargs):
        raise RuntimeError("app.run() must not be reached in unit tests")


class _FailingAgent:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, _query):
        raise RuntimeError(SECRET)

    async def stream_async(self, _query):
        raise RuntimeError(SECRET)
        yield  # makes this an async generator


class _FakeCodeTools:
    def __init__(self, _region):
        pass

    def execute_python_securely(self, code):
        return code


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _load_agent(monkeypatch, pattern, extract_user_id=lambda _ctx: "user-1"):
    stubs = {
        "bedrock_agentcore": _module("bedrock_agentcore"),
        "bedrock_agentcore.runtime": _module(
            "bedrock_agentcore.runtime",
            BedrockAgentCoreApp=_App,
            RequestContext=object,
        ),
        "strands": _module("strands", Agent=_FailingAgent),
        "strands.models": _module("strands.models", BedrockModel=lambda **_kw: None),
        "shared": _module("shared"),
        "shared.auth": _module(
            "shared.auth", extract_user_id_from_context=extract_user_id
        ),
        "shared.jwt_claims": _module(
            "shared.jwt_claims", memory_retrieval_plan=lambda _user: {}
        ),
        "tools": _module("tools"),
        "tools.code_interpreter": _module(
            "tools.code_interpreter", StrandsCodeInterpreterTools=_FakeCodeTools
        ),
        "tools.gateway": _module(
            "tools.gateway", create_gateway_mcp_client=lambda: None
        ),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("MEMORY_ID", raising=False)
    spec = importlib.util.spec_from_file_location(
        f"agent_under_test_{pattern.replace('-', '_')}",
        AGENT_CODE / pattern / "agent.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _collect(agen):
    return [event async for event in agen]


def _assert_generic(response, caplog):
    text = json.dumps(response)
    assert response["status"] == "error"
    assert response["correlation_id"] in response["error"]
    assert SECRET not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text
    # Full details are logged server-side, tagged with the same id.
    assert SECRET in caplog.text
    assert response["correlation_id"] in caplog.text


def test_orchestrator_returns_generic_error(monkeypatch, caplog):
    agent = _load_agent(monkeypatch, "orchestrator")
    response = asyncio.run(agent.invoke({"prompt": "hi"}))
    _assert_generic(response, caplog)


def test_strands_agent_returns_generic_error(monkeypatch, caplog):
    agent = _load_agent(monkeypatch, "strands-agent")
    events = asyncio.run(
        _collect(agent.invocations({"prompt": "hi", "runtimeSessionId": "s"}, None))
    )
    assert len(events) == 1
    _assert_generic(events[0], caplog)


def test_strands_agent_keeps_unauthorized_response(monkeypatch):
    def reject(_ctx):
        raise ValueError(SECRET)

    agent = _load_agent(monkeypatch, "strands-agent", extract_user_id=reject)
    events = asyncio.run(
        _collect(agent.invocations({"prompt": "hi", "runtimeSessionId": "s"}, None))
    )
    assert events == [{"status": "error", "error": "Unauthorized"}]


@pytest.mark.parametrize("pattern", AGENT_PATTERNS)
def test_agent_source_never_echoes_exceptions(pattern):
    source = (AGENT_CODE / pattern / "agent.py").read_text()
    assert not re.search(r"str\((?:e|exc)\)", source)
    assert "type(exc).__name__" not in source
    assert "correlation_id" in source
