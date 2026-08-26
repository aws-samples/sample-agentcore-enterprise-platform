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


def invoke_agent(prompt: str, session_id: str) -> str:
    """POST the prompt to the runtime data plane with a Bearer M2M token."""
    return _post(_runtime_url(), {"prompt": prompt, "runtimeSessionId": session_id}, {})


def invoke_agui(prompt: str, session_id: str) -> str:
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
                print(invoke(args.prompt, session_id))
    except urllib.error.HTTPError as e:
        # Surface the real error — users need the status and body to debug
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
