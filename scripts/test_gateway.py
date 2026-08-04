#!/usr/bin/env python3
"""
Test AgentCore Gateway directly: MCP tools/list plus one tools/call.

Auth is the shared M2M client_credentials flow (utils.get_m2m_token); the
gateway URL comes from SSM /{project}/{env}/gateway/url.

Usage:
    python scripts/test_gateway.py
    python scripts/test_gateway.py --project agentcore-workshop --env dev
"""

import argparse
import json
import sys

import requests
from utils import get_m2m_token, get_ssm_param, print_msg, print_section

HEADERS_ACCEPT = "application/json, text/event-stream"
TOOL_NAME = "sample-tool___text_analysis_tool"


def mcp_request(
    gateway_url: str, token: str, method: str, params: dict | None = None
) -> dict:
    """POST one MCP JSON-RPC request to the gateway."""
    payload = {"jsonrpc": "2.0", "id": method, "method": method}
    if params:
        payload["params"] = params
    response = requests.post(
        gateway_url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": HEADERS_ACCEPT,
        },
        timeout=30,
    )
    if response.status_code != 200:
        print_msg(
            f"Gateway request failed: {response.status_code} - {response.text}", "error"
        )
        sys.exit(1)
    return response.json()


def main():
    parser = argparse.ArgumentParser(description="Test AgentCore Gateway")
    parser.add_argument("--project", default=None, help="Project name")
    parser.add_argument("--env", default=None, help="Environment")
    args = parser.parse_args()

    print_section("AgentCore Gateway Direct Test")

    print("1. Fetching configuration...")
    gateway_url = get_ssm_param("gateway/url", args.project, args.env)
    print_msg(f"Gateway URL: {gateway_url}")

    print("\n2. Getting M2M access token...")
    token = get_m2m_token(args.project, args.env)
    print_msg("Access token obtained")

    print("\n3. Calling tools/list...")
    tools = mcp_request(gateway_url, token, "tools/list")
    tool_list = tools.get("result", {}).get("tools", [])
    if not tool_list:
        print_msg("No tools found in gateway", "error")
        sys.exit(1)
    print_msg(f"{len(tool_list)} tool(s): {', '.join(t['name'] for t in tool_list)}")

    print(f"\n4. Calling {TOOL_NAME}...")
    result = mcp_request(
        gateway_url,
        token,
        "tools/call",
        {"name": TOOL_NAME, "arguments": {"text": "Hello world!", "N": 3}},
    )
    if "error" in result:
        print_msg(f"Tool returned error: {result['error']}", "error")
        sys.exit(1)
    print_msg("Tool call successful")
    print("\nResponse:")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
