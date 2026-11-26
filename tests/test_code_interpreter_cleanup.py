"""Code Interpreter sessions: one per request, always stopped.

agent-code/shared/code_interpreter.py keeps one session for all tool calls of a
request (clearContext is False, so state carries across calls) and the agent
entrypoint stops it. A fake CodeInterpreter stands in for the AgentCore client,
so no AWS call is made.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "shared_code_interpreter_under_test",
    REPO / "agent-code" / "shared" / "code_interpreter.py",
)
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)

SECRET = "internal-detail-s3cret"


@pytest.fixture
def interpreter(monkeypatch):
    """Fake CodeInterpreter class; every session it creates is recorded."""
    created = []

    class FakeCodeInterpreter:
        fail_invoke = False
        fail_stop = False

        def __init__(self, region):
            self.region = region
            self.start_calls = 0
            self.stop_calls = 0
            self.invoke_calls = 0
            created.append(self)

        def start(self):
            self.start_calls += 1

        def invoke(self, _operation, _params):
            self.invoke_calls += 1
            if self.fail_invoke:
                raise RuntimeError(SECRET)
            return {"stream": [{"result": {"content": "ok"}}]}

        def stop(self):
            self.stop_calls += 1
            if self.fail_stop:
                raise RuntimeError("stop failed")

    FakeCodeInterpreter.created = created

    client_mod = types.ModuleType("bedrock_agentcore.tools.code_interpreter_client")
    client_mod.CodeInterpreter = FakeCodeInterpreter
    tools_mod = types.ModuleType("bedrock_agentcore.tools")
    tools_mod.code_interpreter_client = client_mod
    pkg = types.ModuleType("bedrock_agentcore")
    pkg.tools = tools_mod
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", pkg)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.tools", tools_mod)
    monkeypatch.setitem(
        sys.modules, "bedrock_agentcore.tools.code_interpreter_client", client_mod
    )
    return FakeCodeInterpreter


def test_one_session_shared_across_tool_calls_in_a_request(interpreter):
    with ci.CodeInterpreterTools("us-east-1") as tools:
        result = tools.execute_python_securely("x = 1")
        assert json.loads(result) == [{"content": "ok"}]
        tools.execute_python_securely("print(x)")
        (session,) = interpreter.created
        assert session.start_calls == 1
        assert session.stop_calls == 0  # context carries across tool calls
    assert session.invoke_calls == 2
    assert session.stop_calls == 1


def test_session_stopped_once_when_tool_errors(interpreter, caplog):
    interpreter.fail_invoke = True
    with ci.CodeInterpreterTools("us-east-1") as tools:
        result = json.loads(tools.execute_python_securely("boom"))
        tools.execute_python_securely("boom again")
    (session,) = interpreter.created
    assert (session.start_calls, session.stop_calls) == (1, 1)
    assert result["error"] == "Code execution failed"
    assert result["correlation_id"]
    assert SECRET not in json.dumps(result)
    # Details are logged server-side under the same correlation id.
    assert SECRET in caplog.text
    assert result["correlation_id"] in caplog.text


def test_session_stopped_once_when_request_fails(interpreter):
    with pytest.raises(RuntimeError), ci.CodeInterpreterTools("us-east-1") as tools:
        tools.execute_python_securely("print(1)")
        raise RuntimeError("agent run failed")
    (session,) = interpreter.created
    assert (session.start_calls, session.stop_calls) == (1, 1)


def test_entrypoint_finally_stops_session(interpreter):
    tools = ci.CodeInterpreterTools("us-east-1")
    try:
        tools.execute_python_securely("print(1)")
        tools.execute_python_securely("print(2)")
    finally:
        tools.cleanup()
    (session,) = interpreter.created
    assert (session.start_calls, session.stop_calls) == (1, 1)


def test_each_request_gets_its_own_session(interpreter):
    for _ in range(2):
        with ci.CodeInterpreterTools("us-east-1") as tools:
            tools.execute_python_securely("print(1)")
    assert [(s.start_calls, s.stop_calls) for s in interpreter.created] == [
        (1, 1),
        (1, 1),
    ]


def test_failing_stop_does_not_raise(interpreter):
    interpreter.fail_stop = True
    with ci.CodeInterpreterTools("us-east-1") as tools:
        result = tools.execute_python_securely("print(1)")
    assert json.loads(result) == [{"content": "ok"}]
    (session,) = interpreter.created
    assert session.stop_calls == 1


def test_cleanup_is_idempotent_and_noop_without_session(interpreter):
    tools = ci.CodeInterpreterTools("us-east-1")
    tools.cleanup()  # no session opened yet
    assert interpreter.created == []
    tools.execute_python_securely("print(1)")
    tools.cleanup()
    tools.cleanup()
    (session,) = interpreter.created
    assert session.stop_calls == 1
