"""Per-pattern verification of recorded runtime replies.

Each agent pattern streams tool use in its own shape. These fixtures are
recorded payload shapes for every pattern, covering: tool used, tool not
used, JSON-RPC error, failed task and guardrail block.
"""

import json
import sys
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
MARKER = "CODE_INTERPRETER_OK"
CODE = "print('CODE_INTERPRETER_OK')"
BLOCK = "Sorry, your request was blocked by the platform guardrail."
PATTERNS = [
    "orchestrator",
    "strands-agent",
    "langgraph-agent",
    "claude-sdk-agent",
    "claude-sdk-multi-agent",
    "agui-strands-agent",
    "agui-langgraph-agent",
]


def _sse(*events) -> str:
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events)


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
    events = []
    if tool:
        name = f"code-interpreter___{TOOL}"
        events += [
            {
                "messages": [
                    {
                        "type": "ai",
                        "content": "",
                        "tool_calls": [
                            {
                                "name": name,
                                "args": {"code": CODE},
                                "id": "call_1",
                                "type": "tool_call",
                            }
                        ],
                    }
                ]
            },
            {
                "messages": [
                    {
                        "type": "tool",
                        "name": name,
                        "tool_call_id": "call_1",
                        "content": MARKER + "\n",
                        "status": status,
                    }
                ]
            },
        ]
    events.append({"messages": [{"type": "ai", "content": answer}]})
    return _sse(*events)


def _claude(
    answer: str, tool: bool = True, is_error: bool = False, multi: bool = False
) -> str:
    events: list[dict] = [
        {"type": "system", "subtype": "init", "data": {"session_id": "s-1"}}
    ]
    parent = None
    if multi:
        parent = "toolu_task"
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": parent,
                            "name": "Task",
                            "input": {"subagent_type": "coder", "prompt": "run"},
                        }
                    ]
                },
            }
        )
    if tool:
        events += [
            {
                "type": "assistant",
                "parent_tool_use_id": parent,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": f"mcp__platform__{TOOL}",
                            "input": {"code": CODE},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "parent_tool_use_id": parent,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": MARKER}],
                            "is_error": is_error,
                        }
                    ]
                },
            },
        ]
    events += [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": answer}]},
        },
        {"type": "result", "subtype": "success", "result": answer},
    ]
    return _sse(*events)


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
        "guardrail_block": build(BLOCK, tool=False),
    }


def _claude_multi(answer, tool=True):
    return _claude(answer, tool, multi=True)


FIXTURES = {
    "orchestrator": _fixtures(_strands, sse_envelopes=False),
    "strands-agent": _fixtures(_strands, sse_envelopes=False),
    "langgraph-agent": _fixtures(_langgraph, sse_envelopes=False),
    "claude-sdk-agent": _fixtures(_claude, sse_envelopes=True),
    "claude-sdk-multi-agent": _fixtures(_claude_multi, sse_envelopes=True),
    "agui-strands-agent": _fixtures(_agui, sse_envelopes=True),
    "agui-langgraph-agent": _fixtures(_agui, sse_envelopes=True),
}


def test_parser_table_covers_all_seven_patterns():
    assert set(PATTERN_PARSERS) == set(PATTERNS)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_tool_used_passes(pattern):
    body = FIXTURES[pattern]["tool_used"]
    validate_tool_result(body, TOOL, MARKER, pattern=pattern)
    validate_tool_result(body, TOOL, MARKER)  # generic fallback
    check_agent_reply(body)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_tool_not_used_fails_the_tool_check(pattern):
    body = FIXTURES[pattern]["tool_not_used"]
    check_agent_reply(body)
    with pytest.raises(ToolVerificationError, match="was not invoked"):
        validate_tool_result(body, TOOL, MARKER, pattern=pattern)


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
    ],
)
def test_failed_tool_result_fails_per_pattern(pattern, body):
    with pytest.raises(ToolVerificationError, match="required tool failed"):
        validate_tool_result(body, TOOL, MARKER, pattern=pattern)


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

    monkeypatch.setattr(invoke, "_post", lambda *a, **k: _strands(BLOCK, tool=False))
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
