"""Per-pattern verification of recorded runtime replies.

Each agent pattern streams tool use in its own shape. These fixtures copy what
the agents in agent-code/ actually emit, covering: tool used, tool not used,
JSON-RPC error, failed task and guardrail block.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import invoke
from invoke import (
    PATTERN_PARSERS,
    RuntimeVerificationError,
    ToolVerificationError,
    check_agent_reply,
    invoke_agent,
    invoke_agui,
    validate_tool_result,
)

TOOL = "execute_python_securely"
CLAUDE_TOOL = "execute_code"
MARKER = "CODE_INTERPRETER_OK"
CODE = "print('CODE_INTERPRETER_OK')"
_EGRESS = json.loads(
    (REPO / "control-library" / "guardrails" / "egress-default.json").read_text()
)
BLOCKED_INPUT = _EGRESS["BlockedInputMessaging"]
BLOCKED_OUTPUT = _EGRESS["BlockedOutputsMessaging"]
PATTERNS = [
    "orchestrator",
    "strands-agent",
    "langgraph-agent",
    "claude-sdk-agent",
    "claude-sdk-multi-agent",
    "agui-strands-agent",
    "agui-langgraph-agent",
]
# The orchestrator returns one JSON object without tool events, and verify
# does not require a tool from it.
TOOL_PATTERNS = [p for p in PATTERNS if p != "orchestrator"]
REQUIRED_TOOL = {
    **{p: TOOL for p in PATTERNS},
    "claude-sdk-agent": CLAUDE_TOOL,
    "claude-sdk-multi-agent": CLAUDE_TOOL,
}


def _sse(*events) -> str:
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events)


def _orchestrator(answer: str, tool: bool = True) -> str:
    """agent-code/orchestrator/agent.py returns plain JSON."""
    return json.dumps({"status": "success", "response": answer})


def _strands(answer: str, tool: bool = True, status: str = "success") -> str:
    events = []
    if tool:
        events += [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tooluse_1",
                                "name": TOOL,
                                "input": {"code": CODE},
                            }
                        }
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "tooluse_1",
                                "status": status,
                                "content": [{"text": MARKER + "\n"}],
                            }
                        }
                    ],
                }
            },
        ]
    events.append({"message": {"role": "assistant", "content": [{"text": answer}]}})
    return _sse(*events)


def _langgraph(answer: str, tool: bool = True, status: str = "success") -> str:
    """agent-code/langgraph-agent yields one message model_dump() per event."""
    events = []
    if tool:
        name = f"code-interpreter___{TOOL}"
        events += [
            {
                "type": "ai",
                "content": "",
                "additional_kwargs": {},
                "response_metadata": {"stopReason": "tool_use"},
                "id": "run-1",
                "tool_calls": [
                    {
                        "name": name,
                        "args": {"code": CODE},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            },
            {
                "type": "tool",
                "name": name,
                "tool_call_id": "call_1",
                "content": MARKER + "\n",
                "status": status,
                "additional_kwargs": {},
                "response_metadata": {},
                "id": "tool-msg-1",
            },
        ]
    events.append(
        {
            "type": "ai",
            "content": answer,
            "additional_kwargs": {},
            "response_metadata": {"stopReason": "end_turn"},
            "id": "run-2",
            "tool_calls": [],
        }
    )
    return _sse(*events)


def _claude(
    answer: str, tool: bool = True, is_error: bool = False, multi: bool = False
) -> str:
    """What agent-code/claude-sdk-*/agent.py yield, one SSE event per yield."""
    events: list[dict] = []
    if multi:
        events.append(
            {
                "current_tool_use": {
                    "name": "Task",
                    "input": {"subagent_type": "code-analyst", "prompt": "run"},
                    "toolUseId": "tool-toolu_task",
                }
            }
        )
    if tool:
        result = json.dumps({"stdout": MARKER + "\n", "code_int_session_id": "ci-1"})
        events += [
            {
                "current_tool_use": {
                    "name": f"mcp__codeint__{CLAUDE_TOOL}",
                    "input": {"code": CODE, "code_int_session_id": ""},
                    "toolUseId": "tool-toolu_1",
                }
            },
            {
                "tool_result": {
                    "toolUseId": "tool-toolu_1",
                    "content": [{"type": "text", "text": result}],
                    "is_error": is_error,
                }
            },
        ]
    if multi:
        events.append(
            {
                "tool_result": {
                    "toolUseId": "tool-toolu_task",
                    "content": [{"type": "text", "text": "done"}],
                    "is_error": False,
                }
            }
        )
    events += [{"data": answer}, {"claude_session_id": "claude-session-1"}]
    return _sse(*events)


def _claude_multi(answer: str, tool: bool = True, is_error: bool = False) -> str:
    return _claude(answer, tool, is_error, multi=True)


def _agui(answer: str, tool: bool = True) -> str:
    events: list[dict] = [{"type": "RUN_STARTED", "threadId": "t-1", "runId": "r-1"}]
    if tool:
        events += [
            {"type": "TOOL_CALL_START", "toolCallId": "tc-1", "toolCallName": TOOL},
            {
                "type": "TOOL_CALL_ARGS",
                "toolCallId": "tc-1",
                "delta": json.dumps({"code": CODE}),
            },
            {"type": "TOOL_CALL_END", "toolCallId": "tc-1"},
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": "tc-1",
                "messageId": "m-tool",
                "role": "tool",
                "content": MARKER,
            },
        ]
    events += [
        {"type": "TEXT_MESSAGE_START", "messageId": "m-1", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m-1", "delta": answer},
        {"type": "TEXT_MESSAGE_END", "messageId": "m-1"},
        {"type": "RUN_FINISHED", "threadId": "t-1", "runId": "r-1"},
    ]
    return _sse(*events)


JSONRPC_ERROR = {
    "jsonrpc": "2.0",
    "id": "req-1",
    "error": {"code": -32603, "message": "Internal error: model throttled"},
}


def _task(state: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "result": {
            "kind": "task",
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {
                "state": state,
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "Model invocation failed"}],
                },
            },
        },
    }


def _fixtures(build, sse_envelopes: bool) -> dict[str, str]:
    def envelope(payload: dict) -> str:
        return _sse(payload) if sse_envelopes else json.dumps(payload)

    return {
        "tool_used": build("Healthy"),
        "tool_not_used": build("Healthy", tool=False),
        "jsonrpc_error": envelope(JSONRPC_ERROR),
        "failed_task": envelope(_task("failed")),
        "guardrail_block": build(BLOCKED_OUTPUT, tool=False),
    }


FIXTURES = {
    "orchestrator": _fixtures(_orchestrator, sse_envelopes=False),
    "strands-agent": _fixtures(_strands, sse_envelopes=True),
    "langgraph-agent": _fixtures(_langgraph, sse_envelopes=True),
    "claude-sdk-agent": _fixtures(_claude, sse_envelopes=True),
    "claude-sdk-multi-agent": _fixtures(_claude_multi, sse_envelopes=True),
    "agui-strands-agent": _fixtures(_agui, sse_envelopes=True),
    "agui-langgraph-agent": _fixtures(_agui, sse_envelopes=True),
}


def test_parser_table_covers_all_seven_patterns():
    assert set(PATTERN_PARSERS) == set(PATTERNS)


@pytest.mark.parametrize("pattern", TOOL_PATTERNS)
def test_tool_used_passes(pattern):
    body = FIXTURES[pattern]["tool_used"]
    tool = REQUIRED_TOOL[pattern]
    validate_tool_result(body, tool, MARKER, pattern=pattern)
    validate_tool_result(body, tool, MARKER)  # generic fallback
    check_agent_reply(body)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_tool_not_used_fails_the_tool_check(pattern):
    body = FIXTURES[pattern]["tool_not_used"]
    check_agent_reply(body)
    with pytest.raises(ToolVerificationError, match="was not invoked"):
        validate_tool_result(body, REQUIRED_TOOL[pattern], MARKER, pattern=pattern)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_jsonrpc_error_fails(pattern):
    with pytest.raises(RuntimeVerificationError, match="JSON-RPC error: -32603"):
        check_agent_reply(FIXTURES[pattern]["jsonrpc_error"])


@pytest.mark.parametrize("pattern", PATTERNS)
def test_failed_task_fails(pattern):
    with pytest.raises(
        RuntimeVerificationError, match="A2A task ended in state failed"
    ):
        check_agent_reply(FIXTURES[pattern]["failed_task"])


@pytest.mark.parametrize("state", ["rejected", "canceled"])
def test_rejected_and_canceled_tasks_fail(state):
    with pytest.raises(RuntimeVerificationError, match=f"in state {state}"):
        check_agent_reply(json.dumps(_task(state)))


@pytest.mark.parametrize("pattern", PATTERNS)
def test_guardrail_block_fails(pattern):
    with pytest.raises(RuntimeVerificationError, match="guardrail block message"):
        check_agent_reply(FIXTURES[pattern]["guardrail_block"])


def test_guardrail_input_block_fails():
    with pytest.raises(RuntimeVerificationError, match="guardrail block message"):
        check_agent_reply(_strands(BLOCKED_INPUT, tool=False))


def test_an_answer_that_mentions_a_block_is_not_a_block():
    check_agent_reply(_strands("Healthy; no request was blocked.", tool=False))


def test_guardrail_messages_come_from_the_control_library():
    got = invoke.GUARDRAIL_BLOCK_MESSAGES
    assert invoke._normalise(BLOCKED_INPUT) in got
    assert invoke._normalise(BLOCKED_OUTPUT) in got


def test_guardrail_messages_load_every_config():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "a.json").write_text(
            json.dumps(
                {
                    "BlockedInputMessaging": "Nope, input.",
                    "BlockedOutputsMessaging": "Nope,  OUTPUT.",
                }
            )
        )
        (directory / "broken.json").write_text("{")
        got = invoke._guardrail_block_messages(directory)
    assert {"nope, input.", "nope, output."} <= got


@pytest.mark.parametrize(
    "body",
    [
        _strands("Partial answer", tool=False)
        + "\n\n"
        + _sse({"status": "error", "error": "ThrottlingException: slow down"}),
        _langgraph("Partial answer", tool=False)
        + "\n\n"
        + _sse({"status": "error", "error": "ThrottlingException: slow down"}),
        json.dumps({"status": "error", "error": "ThrottlingException: slow down"}),
    ],
)
def test_agent_error_after_partial_text_fails(body):
    with pytest.raises(
        RuntimeVerificationError, match="agent reported an error: Throttling"
    ):
        check_agent_reply(body)


def test_failed_langgraph_tool_message_is_left_to_the_tool_check():
    body = _langgraph("Healthy", status="error")
    check_agent_reply(body)
    with pytest.raises(ToolVerificationError, match="required tool failed"):
        validate_tool_result(body, TOOL, MARKER, pattern="langgraph-agent")


@pytest.mark.parametrize(
    "body",
    [
        "",
        '""',
        _sse({"type": "RUN_STARTED", "threadId": "t", "runId": "r"}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "kind": "task",
                    "status": {"state": "completed"},
                    "history": [
                        {"role": "user", "parts": [{"kind": "text", "text": "Hi"}]}
                    ],
                },
            }
        ),
        _claude(""),
        json.dumps(
            {
                "type": "ai",
                "content": "",
                "additional_kwargs": {"text": "not the answer"},
                "response_metadata": {"content": "not the answer"},
            }
        ),
    ],
)
def test_empty_answer_fails(body):
    with pytest.raises(RuntimeVerificationError, match="empty answer"):
        check_agent_reply(body)


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "1",
                                "name": "search",
                                "input": {"text": "hello"},
                            }
                        }
                    ],
                }
            }
        ),
        json.dumps(
            {
                "toolResult": {
                    "toolUseId": "1",
                    "status": "success",
                    "content": [{"text": "tool output"}],
                }
            }
        ),
        json.dumps(
            {
                "toolUseId": "1",
                "status": "success",
                "content": [{"text": "tool output"}],
            }
        ),
        _strands(""),
    ],
)
def test_strands_tool_blocks_are_not_an_answer(body):
    with pytest.raises(RuntimeVerificationError, match="empty answer"):
        check_agent_reply(body)


def test_completed_a2a_task_with_an_answer_passes():
    check_agent_reply(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "kind": "task",
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"kind": "text", "text": "Healthy"}]}],
                },
            }
        )
    )


@pytest.mark.parametrize(
    ("pattern", "body"),
    [
        ("strands-agent", _strands("Healthy", status="error")),
        ("langgraph-agent", _langgraph("Healthy", status="error")),
        ("claude-sdk-agent", _claude("Healthy", is_error=True)),
        ("claude-sdk-multi-agent", _claude_multi("Healthy", is_error=True)),
    ],
)
def test_failed_tool_result_fails_per_pattern(pattern, body):
    with pytest.raises(ToolVerificationError, match="required tool failed"):
        validate_tool_result(body, REQUIRED_TOOL[pattern], MARKER, pattern=pattern)


def test_invoke_agent_threads_pattern_and_checks_the_reply(monkeypatch):
    body = FIXTURES["langgraph-agent"]["tool_used"]
    monkeypatch.setattr(invoke, "_post", lambda *a, **k: body)
    got = invoke_agent(
        "hi",
        "session-x",
        TOOL,
        MARKER,
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        region="us-east-1",
        pattern="langgraph-agent",
    )
    assert got == body

    monkeypatch.setattr(
        invoke, "_post", lambda *a, **k: _strands(BLOCKED_OUTPUT, tool=False)
    )
    with pytest.raises(RuntimeVerificationError, match="guardrail"):
        invoke_agent(
            "hi",
            "session-x",
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
            region="us-east-1",
        )


def test_invoke_agui_fails_on_run_error(monkeypatch):
    body = _sse(
        {"type": "RUN_STARTED", "threadId": "t", "runId": "r"},
        {"type": "RUN_ERROR", "code": "ThrottlingException", "message": "slow"},
    )
    monkeypatch.setattr(invoke, "_post", lambda *a, **k: body)
    with pytest.raises(RuntimeVerificationError, match="RUN_ERROR Throttling"):
        invoke_agui(
            "hi",
            "session-x",
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
            region="us-east-1",
        )
