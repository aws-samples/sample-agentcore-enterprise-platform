"""Orchestrator agent: no blocking I/O on the event loop, honest docs."""

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = ROOT / "agent-code" / "orchestrator" / "agent.py"


class _FakeApp:
    def entrypoint(self, func):
        return func

    def run(self):  # pragma: no cover - never called
        pass


class _FakeModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _SlowAgent:
    delay = 0.6
    calls: ClassVar[list[str]] = []

    def __init__(self, system_prompt=None, model=None):
        self.system_prompt = system_prompt
        self.model = model

    def __call__(self, query):
        time.sleep(self.delay)  # blocking, like the real synchronous SDK call
        _SlowAgent.calls.append(query)
        return types.SimpleNamespace(message={"content": [{"text": f"echo: {query}"}]})


@pytest.fixture
def orchestrator(monkeypatch):
    bac = types.ModuleType("bedrock_agentcore")
    bac_runtime = types.ModuleType("bedrock_agentcore.runtime")
    bac_runtime.BedrockAgentCoreApp = _FakeApp
    strands = types.ModuleType("strands")
    strands.Agent = _SlowAgent
    strands_models = types.ModuleType("strands.models")
    strands_models.BedrockModel = _FakeModel
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", bac)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.runtime", bac_runtime)
    monkeypatch.setitem(sys.modules, "strands", strands)
    monkeypatch.setitem(sys.modules, "strands.models", strands_models)

    spec = importlib.util.spec_from_file_location(
        "orchestrator_agent_under_test", AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SlowAgent.calls = []
    return module


def test_ping_stays_responsive_during_slow_invocation(orchestrator):
    async def scenario():
        task = asyncio.create_task(orchestrator.invoke({"prompt": "hi"}))
        start = time.monotonic()
        await asyncio.sleep(0.05)  # stand-in for the /ping handler
        ping_latency = time.monotonic() - start
        still_running = not task.done()
        result = await task
        return ping_latency, still_running, result

    ping_latency, still_running, result = asyncio.run(scenario())
    assert ping_latency < 0.3
    assert still_running
    assert result == {"status": "success", "response": "echo: hi"}
    assert _SlowAgent.calls == ["hi"]


def test_default_prompt_when_payload_missing(orchestrator):
    _SlowAgent.delay = 0
    try:
        result = asyncio.run(orchestrator.invoke(None))
    finally:
        _SlowAgent.delay = 0.6
    assert result["response"] == "echo: Hello!"


def test_readme_does_not_claim_delegation():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "it delegates\n# to sub-agents" not in readme
    assert "does not call the A2A sub-agents" in readme


DELEGATION_CLAIMS = (
    "delegating orchestrator",
    "orchestrator (default,\ndelegating",
    "orchestrator calls sub-agents",
    "orchestrator decides what to delegate",
)


def test_docs_do_not_claim_the_orchestrator_delegates():
    paths = [
        *sorted((ROOT / "docs").rglob("*.md")),
        ROOT / "migration" / "simulation" / "DEMO_SCRIPT.md",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for claim in DELEGATION_CLAIMS:
            assert claim not in text, f"{path.relative_to(ROOT)} claims: {claim!r}"
