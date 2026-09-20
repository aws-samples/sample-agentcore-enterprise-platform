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

from utils import get_m2m_token, get_ssm_param, resolve_region

REGION = resolve_region()


class ToolVerificationError(RuntimeError):
    """A required runtime tool was absent, failed, or returned the wrong result."""


class RuntimeVerificationError(RuntimeError):
    """The runtime replied, but its structured application result was a failure."""


def new_session_id() -> str:
    """40-char unique session id (runtime requires >= 33 chars)."""
    return f"session-{uuid.uuid4().hex}"  # 8 + 32 = 40 chars


def _runtime_url(component: str = "orchestrator") -> str:
    runtime_arn = get_ssm_param(f"runtimes/{component}/arn")
    return (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    )


def _post(url: str, payload: dict, headers: dict) -> str:
    token = get_m2m_token()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **headers,
        },
    )
    return urllib.request.urlopen(req).read().decode()  # nosec B310 — https URL built above


def invoke_agent(
    prompt: str,
    session_id: str,
    required_tool: str | None = None,
    required_result: str | None = None,
    require_success: bool = False,
) -> str:
    """POST the prompt to the runtime data plane with a Bearer M2M token."""
    body = _post(_runtime_url(), {"prompt": prompt, "runtimeSessionId": session_id}, {})
    if required_tool:
        validate_tool_result(body, required_tool, required_result)
    if require_success:
        validate_runtime_success(body)
    return body


def invoke_agui(
    prompt: str,
    session_id: str,
    required_tool: str | None = None,
    required_result: str | None = None,
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
        _runtime_url(),
        payload,
        {
            "Accept": "text/event-stream",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    if required_tool:
        validate_tool_result(body, required_tool, required_result)

    # Report the assistant's text, and surface RUN_ERROR rather than printing
    # an empty stream when the agent fails.
    text, errors = [], []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[len("data:") :].strip())
        except json.JSONDecodeError:
            continue
        if event.get("type") == "TEXT_MESSAGE_CONTENT":
            text.append(event.get("delta", ""))
        elif event.get("type") == "RUN_ERROR":
            errors.append(f"{event.get('code')}: {event.get('message')}")
    out = ["AGENT TEXT: " + repr("".join(text))] if text else []
    out += [f"RUN_ERROR {e}" for e in errors]
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


def validate_tool_result(
    body: str, required_tool: str, required_result: str | None = None
) -> None:
    """Require one successful structured result from a named runtime tool.

    Strands HTTP streams expose ``toolUse``/``toolResult`` objects, while
    AG-UI uses ``TOOL_CALL_START``/``TOOL_CALL_RESULT`` events. Inspect both
    forms so a friendly final answer cannot hide a failed dependency.
    """
    call_ids: set[str] = set()
    results: dict[str, list[tuple[bool, str]]] = {}

    for event in _sse_events(body):
        if event.get("type") == "TOOL_CALL_START":
            if event.get("toolCallName") == required_tool and event.get("toolCallId"):
                call_ids.add(str(event["toolCallId"]))
        elif event.get("type") == "TOOL_CALL_RESULT" and event.get("toolCallId"):
            call_id = str(event["toolCallId"])
            results.setdefault(call_id, []).append(
                (False, json.dumps(event.get("content", ""), default=str))
            )

        for node in _walk_json(event):
            tool_use = node.get("toolUse")
            if (
                isinstance(tool_use, dict)
                and tool_use.get("name") == required_tool
                and tool_use.get("toolUseId")
            ):
                call_ids.add(str(tool_use["toolUseId"]))

            tool_result = node.get("toolResult")
            if isinstance(tool_result, dict) and tool_result.get("toolUseId"):
                call_id = str(tool_result["toolUseId"])
                failed = (
                    str(tool_result.get("status", "")).lower() == "error"
                    or tool_result.get("isError") is True
                )
                results.setdefault(call_id, []).append(
                    (
                        failed,
                        json.dumps(tool_result.get("content", ""), default=str),
                    )
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


def invoke_a2a(prompt: str, session_id: str, component: str) -> str:
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
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=get_ssm_param(f"runtimes/{component}/arn"),
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        payload=json.dumps(envelope).encode(),
    )
    body = response["response"].read().decode()
    try:
        result = json.loads(body).get("result", {})
    except json.JSONDecodeError:
        return body
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                return "AGENT TEXT: " + part["text"]
    # A task without artifacts still proves the contract; show the status.
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
    return urllib.request.urlopen(req).read().decode()  # nosec B310 — https URL from SSM


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
            else:
                invoke = invoke_agui if args.agui else invoke_agent
                print(
                    invoke(
                        args.prompt,
                        session_id,
                        args.require_tool,
                        args.require_tool_result,
                        args.require_success,
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
