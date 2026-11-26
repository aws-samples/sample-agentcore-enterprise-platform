"""Code Interpreter sessions are always stopped — agent-code/shared/code_interpreter.py.

A fake client stands in for the AgentCore CodeInterpreter so no AWS call is
made. The module only imports the real client lazily, so it loads directly.
"""

import importlib.util
import json
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


class FakeClient:
    def __init__(self, fail_invoke=False, fail_stop=False):
        self.fail_invoke = fail_invoke
        self.fail_stop = fail_stop
        self.stop_calls = 0

    def invoke(self, _operation, _params):
        if self.fail_invoke:
            raise RuntimeError(SECRET)
        return {"stream": [{"result": {"content": "ok"}}]}

    def stop(self):
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("stop failed")


def _tools(client):
    tools = ci.CodeInterpreterTools("us-east-1")
    tools._code_client = client
    return tools


def test_session_stopped_on_success():
    client = FakeClient()
    tools = _tools(client)
    result = tools.execute_python_securely("print(1)")
    assert json.loads(result) == [{"content": "ok"}]
    assert client.stop_calls == 1
    assert tools._code_client is None


def test_session_stopped_on_error_and_error_is_generic(caplog):
    client = FakeClient(fail_invoke=True)
    tools = _tools(client)
    result = json.loads(tools.execute_python_securely("boom"))
    assert client.stop_calls == 1
    assert result["error"] == "Code execution failed"
    assert result["correlation_id"]
    assert SECRET not in json.dumps(result)
    # Details are logged server-side under the same correlation id.
    assert SECRET in caplog.text
    assert result["correlation_id"] in caplog.text


def test_failing_stop_does_not_mask_result():
    client = FakeClient(fail_stop=True)
    result = _tools(client).execute_python_securely("print(1)")
    assert json.loads(result) == [{"content": "ok"}]
    assert client.stop_calls == 1


def test_cleanup_is_idempotent():
    client = FakeClient()
    tools = _tools(client)
    tools.cleanup()
    tools.cleanup()
    assert client.stop_calls == 1


def test_context_manager_stops_session_on_success_and_error():
    client = FakeClient()
    with ci.CodeInterpreterTools("us-east-1") as tools:
        tools._code_client = client
    assert client.stop_calls == 1

    client = FakeClient()
    with pytest.raises(RuntimeError), ci.CodeInterpreterTools("us-east-1") as tools:
        tools._code_client = client
        raise RuntimeError("caller failed")
    assert client.stop_calls == 1
