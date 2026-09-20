"""Live invoke verification must not accept a friendly answer after tool failure."""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from invoke import ToolVerificationError, validate_tool_result


def _sse(*events: dict) -> str:
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events)


def _strands_result(*, status: str, content: str) -> str:
    return _sse(
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tool-1",
                            "name": "execute_python_securely",
                            "input": {"code": "print('CODE_INTERPRETER_OK')"},
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
                            "toolUseId": "tool-1",
                            "status": status,
                            "content": [{"text": content}],
                        }
                    }
                ],
            }
        },
        {"message": {"role": "assistant", "content": [{"text": "Healthy"}]}},
    )


def test_accepts_successful_strands_tool_result_with_marker():
    validate_tool_result(
        _strands_result(status="success", content="CODE_INTERPRETER_OK"),
        "execute_python_securely",
        "CODE_INTERPRETER_OK",
    )


def test_rejects_tool_error_even_when_agent_returns_friendly_answer():
    with pytest.raises(ToolVerificationError, match="required tool failed"):
        validate_tool_result(
            _strands_result(status="error", content="AccessDeniedException"),
            "execute_python_securely",
            "CODE_INTERPRETER_OK",
        )


def test_rejects_success_without_expected_result_marker():
    with pytest.raises(ToolVerificationError, match="omitted marker"):
        validate_tool_result(
            _strands_result(status="success", content="unexpected output"),
            "execute_python_securely",
            "CODE_INTERPRETER_OK",
        )


def test_accepts_agui_tool_events():
    validate_tool_result(
        _sse(
            {
                "type": "TOOL_CALL_START",
                "toolCallId": "tool-1",
                "toolCallName": "execute_python_securely",
            },
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": "tool-1",
                "content": "CODE_INTERPRETER_OK",
            },
        ),
        "execute_python_securely",
        "CODE_INTERPRETER_OK",
    )
