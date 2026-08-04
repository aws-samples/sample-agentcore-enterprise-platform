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


def invoke_agent(prompt: str, session_id: str) -> str:
    """POST the prompt to the runtime data plane with a Bearer M2M token."""
    token = get_m2m_token()
    runtime_arn = get_ssm_param("runtimes/orchestrator/arn")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps({"prompt": prompt, "runtimeSessionId": session_id}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    return urllib.request.urlopen(req).read().decode()  # nosec B310 — https URL built above


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
    args = parser.parse_args()
    if not args.tools and not args.prompt:
        parser.error("prompt is required unless --tools is given")

    try:
        if args.tools:
            print(list_tools())
        else:
            session_id = args.session or new_session_id()
            print(f"Session: {session_id}")
            print(invoke_agent(args.prompt, session_id))
    except urllib.error.HTTPError as e:
        # Surface the real error — users need the status and body to debug
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
