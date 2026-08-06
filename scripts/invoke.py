#!/usr/bin/env python3
"""
Invoke the deployed orchestrator agent with one command.

Gets an M2M token (Cognito client_credentials) and POSTs the prompt to the
AgentCore Runtime data plane. With --tools, lists the gateway's MCP tools
instead.

Usage:
    python scripts/invoke.py "What tools do you have?"
    python scripts/invoke.py "And my previous question?" --session <session-id>
    python scripts/invoke.py --tools

Respects AWS_REGION / PROJECT_NAME / ENVIRONMENT env vars.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from utils import get_m2m_token, get_ssm_param

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def new_session_id() -> str:
    """40-char unique session id (runtime requires >= 33 chars)."""
    return f"session-{uuid.uuid4().hex}"  # 8 + 32 = 40 chars


def _runtime_url() -> str:
    runtime_arn = get_ssm_param("runtimes/orchestrator/arn")
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
    args = parser.parse_args()
    if not args.tools and not args.prompt:
        parser.error("prompt is required unless --tools is given")

    try:
        if args.tools:
            print(list_tools())
        else:
            session_id = args.session or new_session_id()
            print(f"Session: {session_id}")
            invoke = invoke_agui if args.agui else invoke_agent
            print(invoke(args.prompt, session_id))
    except urllib.error.HTTPError as e:
        # Surface the real error — users need the status and body to debug
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
