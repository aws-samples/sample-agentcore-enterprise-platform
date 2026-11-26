#!/usr/bin/env python3
"""
Invoke the deployed orchestrator agent with one command.

Gets an M2M token (Cognito client_credentials) and POSTs the prompt to the
AgentCore Runtime data plane. With --tools, lists the gateway's MCP tools
instead.

Usage:
    python scripts/invoke.py "Hello! What kinds of tasks can you help with?"
    python scripts/invoke.py "And my previous question?" --session <session-id>
    python scripts/invoke.py --tools

Respects AWS_REGION / PROJECT_NAME / ENVIRONMENT env vars.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from typing import NamedTuple

from utils import get_m2m_token, get_ssm_param, resolve_region

REGION = resolve_region()
MAX_RUNTIME_RESPONSE_BYTES = 1024 * 1024

# Replies that arrive as HTTP 2xx but are really a guardrail refusal. Matched
# case-insensitively against the agent's answer text.
GUARDRAIL_BLOCK_PHRASES = (
    "request was blocked",
    "blocked by guardrail",
    "blocked by the guardrail",
    "sorry, the model cannot answer this question",
)
# A2A task states that mean the agent did not do the work.
FAILED_TASK_STATES = frozenset({"failed", "rejected", "canceled", "cancelled"})


class ToolVerificationError(RuntimeError):
    """A required runtime tool was absent, failed, or returned the wrong result."""


class RuntimeVerificationError(RuntimeError):
    """The runtime replied, but its structured application result was a failure."""


def _read_bounded(response, limit: int = MAX_RUNTIME_RESPONSE_BYTES) -> str:
    """Read a response without allowing an unbounded runtime payload."""
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeVerificationError(
            f"runtime response exceeded the {limit}-byte safety limit"
        )
    return body.decode()


def new_session_id() -> str:
    """40-char unique session id (runtime requires >= 33 chars)."""
    return f"session-{uuid.uuid4().hex}"  # 8 + 32 = 40 chars


def _runtime_url(
    component: str = "orchestrator",
    *,
    runtime_arn: str | None = None,
    region: str | None = None,
) -> str:
    runtime_arn = runtime_arn or get_ssm_param(f"runtimes/{component}/arn")
    region = region or REGION
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    )


def _post(
    url: str,
    payload: dict,
    headers: dict,
    *,
    region: str | None = None,
    max_response_bytes: int = MAX_RUNTIME_RESPONSE_BYTES,
) -> str:
    token = get_m2m_token(region=region)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **headers,
        },
    )
    response = urllib.request.urlopen(  # nosec B310 — https URL built above
        req,
        timeout=180,
    )
    return _read_bounded(response, max_response_bytes)


def invoke_agent(
    prompt: str,
    session_id: str,
    required_tool: str | None = None,
    required_result: str | None = None,
    require_success: bool = False,
    *,
    runtime_arn: str | None = None,
    region: str | None = None,
    max_response_bytes: int = MAX_RUNTIME_RESPONSE_BYTES,
    pattern: str | None = None,
) -> str:
    """POST the prompt to the runtime data plane with a Bearer M2M token."""
    body = _post(
        _runtime_url(runtime_arn=runtime_arn, region=region),
        {"prompt": prompt, "runtimeSessionId": session_id},
        {},
        region=region,
        max_response_bytes=max_response_bytes,
    )
    # A migrated image may answer with a bare status object, so only the
    # pattern-owned runtimes must produce answer text.
    check_agent_reply(body, require_answer=not require_success)
    if required_tool:
        validate_tool_result(body, required_tool, required_result, pattern)
    if require_success:
        validate_runtime_success(body)
    return body


def invoke_agui(
    prompt: str,
    session_id: str,
    required_tool: str | None = None,
    required_result: str | None = None,
    *,
    runtime_arn: str | None = None,
    region: str | None = None,
    max_response_bytes: int = MAX_RUNTIME_RESPONSE_BYTES,
    pattern: str | None = None,
) -> str:
    """Invoke an AG-UI runtime (agui-* patterns) and print its text deltas.

    AG-UI entrypoints validate the payload as a RunAgentInput, so the prompt has
    to be a message list rather than {"prompt": ...}, the session travels in a
    header, and the reply is an SSE stream of typed events.
    """
    payload = {
        "threadId": session_id,
        "runId": str(uuid.uuid4()),
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": prompt}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    body = _post(
        _runtime_url(runtime_arn=runtime_arn, region=region),
        payload,
        {
            "Accept": "text/event-stream",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
        region=region,
        max_response_bytes=max_response_bytes,
    )

    # Report the assistant's text, and fail on RUN_ERROR rather than printing
    # an empty stream when the agent fails.
    text, errors = [], []
    for event in _sse_events(body):
        if not isinstance(event, dict):
            continue
        if event.get("type") == "TEXT_MESSAGE_CONTENT":
            text.append(event.get("delta", ""))
        elif event.get("type") == "RUN_ERROR":
            errors.append(f"{event.get('code')}: {event.get('message')}")
    if errors:
        raise RuntimeVerificationError(f"agent run failed with RUN_ERROR {errors[0]}")
    check_agent_reply(body)
    if required_tool:
        validate_tool_result(body, required_tool, required_result, pattern)
    out = ["AGENT TEXT: " + repr("".join(text))] if text else []
    return "\n".join(out) or body


def _walk_json(value):
    """Yield every object in a decoded event without assuming one SDK shape."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _sse_events(body: str):
    """Decode JSON SSE events while ignoring keepalives and text fragments."""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            yield json.loads(line[len("data:") :].strip())
        except json.JSONDecodeError:
            continue


def _decoded_runtime_payloads(body: str):
    """Yield a raw JSON response or each JSON SSE event."""
    try:
        yield json.loads(body)
        return
    except json.JSONDecodeError:
        pass
    yield from _sse_events(body)


def validate_runtime_success(body: str) -> None:
    """Require a structured migration response that does not report failure.

    The migration adapter reports child/process failures as JSON with
    ``status=error`` and a 4xx/5xx-style ``code``. AgentCore can carry that
    application payload inside an otherwise successful HTTP response, so
    transport success alone is not enough for a migration health check.
    """
    payloads = list(_decoded_runtime_payloads(body))
    if not payloads:
        raise RuntimeVerificationError(
            "runtime returned no decodable JSON response or SSE event"
        )
    for payload in payloads:
        for node in _walk_json(payload):
            status = str(node.get("status", "")).lower()
            code = node.get("code")
            try:
                failed_code = not isinstance(code, bool) and int(code) >= 400
            except (TypeError, ValueError):
                failed_code = False
            if status in {"error", "failed", "failure"} or (
                failed_code and node.get("error")
            ):
                detail = str(node.get("error") or status or f"code {code}")
                raise RuntimeVerificationError(
                    f"runtime reported an application failure: {detail[:500]}"
                )


def _runtime_payloads(body: str):
    """Decoded payloads, unwrapping SSE events that carry JSON as a string."""
    for payload in _decoded_runtime_payloads(body):
        if isinstance(payload, str) and payload.lstrip()[:1] in ("{", "["):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
        yield payload


# Keys whose string values are the agent's answer, and message shapes that are
# not the agent speaking (prompt echo, tool calls/results, system events).
_ANSWER_KEYS = frozenset(
    {"text", "delta", "data", "content", "response", "answer", "result", "output"}
)
_NON_ANSWER_ROLES = frozenset({"user", "human", "tool", "system"})
_NON_ANSWER_TYPES = frozenset(
    {
        "human",
        "user",
        "tool",
        "system",
        "tool_use",
        "tool_result",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_RESULT",
    }
)


def _collect_text(value, out: list[str], answer_field: bool = True) -> None:
    if isinstance(value, str):
        if answer_field:
            out.append(value)
    elif isinstance(value, list):
        for child in value:
            _collect_text(child, out, answer_field)
    elif isinstance(value, dict):
        kind = value.get("type")
        if str(value.get("role", "")).lower() in _NON_ANSWER_ROLES or (
            isinstance(kind, str) and kind in _NON_ANSWER_TYPES
        ):
            return
        for key, child in value.items():
            _collect_text(child, out, key in _ANSWER_KEYS)


def agent_answer_text(body: str) -> str:
    """Best-effort answer text across the JSON, SSE and plain-text replies."""
    payloads = list(_runtime_payloads(body))
    if not payloads and body.strip():
        payloads = [body]
    out: list[str] = []
    for payload in payloads:
        _collect_text(payload, out)
    return "".join(out)


def _is_rpc_error(error) -> bool:
    return isinstance(error, dict) and "code" in error and "message" in error


def check_agent_reply(body: str, *, require_answer: bool = True) -> None:
    """Fail on application-level failures carried inside an HTTP 2xx reply.

    A JSON-RPC ``error`` member, an A2A task in a failed/rejected/canceled
    state, a guardrail block message and an empty answer all mean the agent
    did not answer, whatever the transport status said.
    """
    payloads = list(_runtime_payloads(body))
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("error") is None:
            continue
        error = payload["error"]
        if "jsonrpc" in payload or _is_rpc_error(error):
            if isinstance(error, dict):
                detail = f"{error.get('code')} {error.get('message')}"
            else:
                detail = str(error)
            raise RuntimeVerificationError(
                f"agent returned a JSON-RPC error: {detail[:500]}"
            )
    for payload in payloads:
        for node in _walk_json(payload):
            status = node.get("status")
            if not isinstance(status, dict):
                continue
            state = str(status.get("state", "")).lower().removeprefix("task_state_")
            if state in FAILED_TASK_STATES:
                parts: list[str] = []
                _collect_text(status.get("message"), parts)
                detail = " ".join(parts) or "no status message"
                raise RuntimeVerificationError(
                    f"A2A task ended in state {state}: {detail[:500]}"
                )
    answer = agent_answer_text(body)
    lowered = answer.lower()
    if any(phrase in lowered for phrase in GUARDRAIL_BLOCK_PHRASES):
        raise RuntimeVerificationError(
            f"agent reply is a guardrail block message: {answer.strip()[:200]!r}"
        )
    if require_answer and not answer.strip():
        raise RuntimeVerificationError("agent returned an empty answer")


class ToolCall(NamedTuple):
    call_id: str
    name: str


class ToolResult(NamedTuple):
    call_id: str
    failed: bool
    content: str


Parser = Callable[[dict], Iterator[ToolCall | ToolResult]]


def _content(value) -> str:
    return json.dumps(value, default=str)


def _strands_records(node: dict):
    """Strands: ``toolUse`` / ``toolResult`` content blocks."""
    tool_use = node.get("toolUse")
    if (
        isinstance(tool_use, dict)
        and tool_use.get("toolUseId")
        and tool_use.get("name")
    ):
        yield ToolCall(str(tool_use["toolUseId"]), str(tool_use["name"]))
    tool_result = node.get("toolResult")
    if isinstance(tool_result, dict) and tool_result.get("toolUseId"):
        failed = (
            str(tool_result.get("status", "")).lower() == "error"
            or tool_result.get("isError") is True
        )
        yield ToolResult(
            str(tool_result["toolUseId"]),
            failed,
            _content(tool_result.get("content", "")),
        )


def _agui_records(node: dict):
    """AG-UI: ``TOOL_CALL_START`` / ``TOOL_CALL_RESULT`` events."""
    kind = node.get("type")
    if (
        kind == "TOOL_CALL_START"
        and node.get("toolCallId")
        and node.get("toolCallName")
    ):
        yield ToolCall(str(node["toolCallId"]), str(node["toolCallName"]))
    elif kind == "TOOL_CALL_RESULT" and node.get("toolCallId"):
        yield ToolResult(
            str(node["toolCallId"]), False, _content(node.get("content", ""))
        )


def _langgraph_records(node: dict):
    """LangGraph: AIMessage ``tool_calls`` and ToolMessage ``tool_call_id``."""
    tool_calls = node.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            name = call.get("name") or (
                function.get("name") if isinstance(function, dict) else None
            )
            if call.get("id") and name:
                yield ToolCall(str(call["id"]), str(name))
    if node.get("tool_call_id") and "content" in node:
        yield ToolResult(
            str(node["tool_call_id"]),
            str(node.get("status", "")).lower() == "error",
            _content(node.get("content", "")),
        )


def _claude_sdk_records(node: dict):
    """Claude Agent SDK: ``tool_use`` / ``tool_result`` blocks."""
    is_call = node.get("type") == "tool_use" or {"id", "name", "input"} <= node.keys()
    if is_call and node.get("id") and node.get("name"):
        yield ToolCall(str(node["id"]), str(node["name"]))
    if node.get("tool_use_id") and (
        node.get("type") == "tool_result" or "content" in node
    ):
        yield ToolResult(
            str(node["tool_use_id"]),
            node.get("is_error") is True,
            _content(node.get("content", "")),
        )


# Which stream/response shapes each agent pattern emits.
PATTERN_PARSERS: dict[str, tuple[Parser, ...]] = {
    "orchestrator": (_strands_records,),
    "strands-agent": (_strands_records,),
    # LangChain Bedrock/Anthropic AIMessages may also carry tool_use blocks.
    "langgraph-agent": (_langgraph_records, _claude_sdk_records),
    "claude-sdk-agent": (_claude_sdk_records,),
    "claude-sdk-multi-agent": (_claude_sdk_records,),
    "agui-strands-agent": (_agui_records,),
    "agui-langgraph-agent": (_agui_records,),
}
ALL_PARSERS: tuple[Parser, ...] = (
    _strands_records,
    _agui_records,
    _langgraph_records,
    _claude_sdk_records,
)


def _tool_matches(name: str, required_tool: str) -> bool:
    """Exact name, or an MCP/gateway-prefixed one (``x__tool``, ``x___tool``)."""
    return name == required_tool or name.endswith(f"__{required_tool}")


def validate_tool_result(
    body: str,
    required_tool: str,
    required_result: str | None = None,
    pattern: str | None = None,
) -> None:
    """Require one successful structured result from a named runtime tool.

    The pattern selects the parsers in PATTERN_PARSERS for its stream format;
    without a known pattern every parser is tried. A friendly final answer
    therefore cannot hide a failed dependency.
    """
    parsers = PATTERN_PARSERS.get(pattern or "", ALL_PARSERS)
    call_ids: set[str] = set()
    results: dict[str, list[tuple[bool, str]]] = {}

    for payload in _runtime_payloads(body):
        for node in _walk_json(payload):
            for parser in parsers:
                for record in parser(node):
                    if isinstance(record, ToolCall):
                        if _tool_matches(record.name, required_tool):
                            call_ids.add(record.call_id)
                    else:
                        results.setdefault(record.call_id, []).append(
                            (record.failed, record.content)
                        )

    if not call_ids:
        raise ToolVerificationError(f"required tool was not invoked: {required_tool}")

    matching_results = [
        result for call_id in call_ids for result in results.get(call_id, [])
    ]
    if not matching_results:
        raise ToolVerificationError(
            f"required tool returned no structured result: {required_tool}"
        )
    if any(failed for failed, _ in matching_results):
        raise ToolVerificationError(f"required tool failed: {required_tool}")
    if required_result and not any(
        required_result in content for _, content in matching_results
    ):
        raise ToolVerificationError(
            f"required tool result omitted marker: {required_tool}"
        )


def invoke_a2a(
    prompt: str,
    session_id: str,
    component: str,
    *,
    runtime_arn: str | None = None,
    region: str | None = None,
    max_response_bytes: int = MAX_RUNTIME_RESPONSE_BYTES,
) -> str:
    """Invoke an A2A sub-agent runtime (code-agent / research-agent).

    Two things differ from invoking the orchestrator, and getting either wrong
    produces an error that looks like a broken agent:

    * **Payload**: A2A runtimes speak JSON-RPC 2.0 (`message/send`), not
      `{"prompt": ...}`. A mismatch surfaces as HTTP 424.
    * **Auth**: A2A is not a client-facing protocol, so these runtimes get no
      inbound JWT authorizer and are guarded by IAM instead — they need SigV4,
      and a Bearer token is rejected with "Authorization method mismatch".
      (See infra_utils/runtime_protocol.py and docs/IDENTITY.md.)

    This is what module 8's verify runs, so the contract cannot silently rot.
    """
    import boto3  # local import: only this path needs it

    envelope = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
                "messageId": str(uuid.uuid4()),
            }
        },
    }
    region = region or REGION
    runtime_arn = runtime_arn or get_ssm_param(f"runtimes/{component}/arn")
    client = boto3.client("bedrock-agentcore", region_name=region)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        payload=json.dumps(envelope).encode(),
    )
    body = _read_bounded(response["response"], max_response_bytes)
    check_agent_reply(body)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body
    result = parsed.get("result") if isinstance(parsed, dict) else None
    if not isinstance(result, dict):
        result = {}
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                return "AGENT TEXT: " + part["text"]
    # A task without artifacts that did not fail (checked above) still proves
    # the contract; show the status.
    return f"A2A RESULT (no artifact): {json.dumps(result)[:400]}"


def list_tools() -> str:
    """POST MCP tools/list to the gateway with a Bearer M2M token."""
    token = get_m2m_token()
    gateway_url = get_ssm_param("gateway/url")
    req = urllib.request.Request(
        gateway_url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    response = urllib.request.urlopen(  # nosec B310 — https URL from SSM
        req,
        timeout=180,
    )
    return _read_bounded(response)


def main():
    parser = argparse.ArgumentParser(
        description="Invoke the deployed orchestrator agent"
    )
    parser.add_argument("prompt", nargs="?", help="Prompt to send to the agent")
    parser.add_argument(
        "--session", default=None, help="Session ID to reuse (>=33 chars)"
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="List the gateway's MCP tools instead of invoking the agent",
    )
    parser.add_argument(
        "--agui",
        action="store_true",
        help="Use the AG-UI protocol (required for the agui-* agent patterns)",
    )
    parser.add_argument(
        "--a2a",
        metavar="COMPONENT",
        nargs="?",
        const="code-agent",
        default=None,
        help="Invoke an A2A sub-agent over JSON-RPC (code-agent|research-agent)",
    )
    parser.add_argument(
        "--require-tool",
        help="Fail unless this runtime tool returns a successful structured result",
    )
    parser.add_argument(
        "--require-tool-result",
        help="Also require this marker inside the named tool's structured result",
    )
    parser.add_argument(
        "--require-success",
        action="store_true",
        help="Fail when the structured runtime response reports an application error",
    )
    parser.add_argument(
        "--pattern",
        default=None,
        help="Agent pattern whose stream format the tool check parses "
        "(e.g. langgraph-agent); unknown or absent tries every format",
    )
    args = parser.parse_args()
    if not args.tools and not args.prompt:
        parser.error("prompt is required unless --tools is given")

    try:
        if args.tools:
            print(list_tools())
        else:
            session_id = args.session or new_session_id()
            print(f"Session: {session_id}")
            if args.a2a:
                print(invoke_a2a(args.prompt, session_id, args.a2a))
            elif args.agui:
                print(
                    invoke_agui(
                        args.prompt,
                        session_id,
                        args.require_tool,
                        args.require_tool_result,
                        pattern=args.pattern,
                    )
                )
            else:
                print(
                    invoke_agent(
                        args.prompt,
                        session_id,
                        args.require_tool,
                        args.require_tool_result,
                        args.require_success,
                        pattern=args.pattern,
                    )
                )
    except urllib.error.HTTPError as e:
        # Surface the real error — users need the status and body to debug
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeVerificationError, ToolVerificationError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
