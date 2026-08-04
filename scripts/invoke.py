#!/usr/bin/env python3
"""
Invoke the deployed orchestrator agent with one command.

Gets an M2M token (Cognito client_credentials) and POSTs the prompt to the
AgentCore Runtime data plane.

Usage:
    python scripts/invoke.py "What tools do you have?"
    python scripts/invoke.py "And my previous question?" --session <session-id>

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
        data=json.dumps({"prompt": prompt}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    return urllib.request.urlopen(req).read().decode()  # nosec B310 — https URL built above


def main():
    parser = argparse.ArgumentParser(
        description="Invoke the deployed orchestrator agent"
    )
    parser.add_argument("prompt", help="Prompt to send to the agent")
    parser.add_argument(
        "--session", default=None, help="Session ID to reuse (>=33 chars)"
    )
    args = parser.parse_args()

    session_id = args.session or str(
        uuid.uuid4()
    )  # uuid4 = 36 chars, meets >=33 minimum
    print(f"Session: {session_id}")

    try:
        print(invoke_agent(args.prompt, session_id))
    except urllib.error.HTTPError as e:
        # Surface the real error — users need the status and body to debug
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
