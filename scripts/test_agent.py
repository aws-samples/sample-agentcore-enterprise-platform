#!/usr/bin/env python3
# Adapted from fullstack-solution-template-for-agentcore
"""
Interactive agent chat tester for deployed AgentCore runtimes.

Tests agent invocation with conversation continuity via Cognito authentication.
Reads configuration from SSM parameters using /{project}/{env}/ convention.

Usage:
    python scripts/test_agent.py
    python scripts/test_agent.py --component orchestrator
    python scripts/test_agent.py --project agentcore-workshop --env dev
"""

import argparse
import getpass
import json
import signal
import sys
import time
from typing import Dict, Optional

import requests
from colorama import Fore, Style

from utils import (
    authenticate_cognito,
    generate_session_id,
    get_ssm_param,
    get_workshop_config,
    print_msg,
    print_section,
)


def generate_trace_id() -> str:
    """Generate X-Amzn-Trace-Id header value."""
    return f"1-{format(int(time.time()), 'x')}-{generate_session_id()}"


def invoke_agent(
    url: str,
    prompt: str,
    session_id: str,
    headers: Dict[str, str],
) -> None:
    """Invoke agent and print streaming events in real-time."""
    payload = {"prompt": prompt, "runtimeSessionId": session_id}
    headers["Content-Type"] = "application/json"

    try:
        response = requests.post(url, headers=headers, json=payload, stream=True, timeout=60)
        if response.status_code != 200:
            print(f"Error: HTTP {response.status_code}: {response.text}")
            return

        print(f"{Fore.GREEN}Agent:{Style.RESET_ALL} ", end="", flush=True)
        printed_something = False
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue

            # SSE format: "data: {...}"
            if line.startswith("data: "):
                try:
                    chunk = json.loads(line[6:])

                    # Strands: text token
                    if isinstance(chunk.get("data"), str):
                        print(chunk["data"], end="", flush=True)
                        printed_something = True
                    # JSON response envelope
                    elif isinstance(chunk.get("response"), str):
                        print(chunk["response"], end="", flush=True)
                        printed_something = True
                    # Strands: tool use
                    elif chunk.get("current_tool_use", {}).get("name"):
                        tool = chunk["current_tool_use"]
                        if chunk.get("delta", {}).get("toolUse", {}).get("input") == "":
                            print(f"\n{Fore.YELLOW}[Tool: {tool['name']}]{Style.RESET_ALL} ", end="", flush=True)
                            printed_something = True
                    # Strands: tool result
                    elif chunk.get("message", {}).get("role") == "user":
                        for content in chunk["message"].get("content", []):
                            if "toolResult" in content:
                                result = str(content["toolResult"].get("content", ""))[:200]
                                print(f"\n{Fore.YELLOW}[Result: {result}]{Style.RESET_ALL}", flush=True)
                                printed_something = True
                except (json.JSONDecodeError, KeyError):
                    continue
            else:
                # Non-SSE: try parsing as raw JSON (runtime returns {"status":"success","response":"..."})
                try:
                    data = json.loads(line)
                    if isinstance(data.get("response"), str):
                        print(data["response"], end="", flush=True)
                        printed_something = True
                    elif isinstance(data.get("data"), str):
                        print(data["data"], end="", flush=True)
                        printed_something = True
                except (json.JSONDecodeError, KeyError):
                    # Plain text fallback
                    print(line, end="", flush=True)
                    printed_something = True
        print()

    except requests.exceptions.ConnectionError:
        print_msg(f"Could not connect to {url}", "error")
        sys.exit(1)


def main():
    print("=" * 60)
    print("AgentCore Workshop - Interactive Chat Tester")
    print("=" * 60 + "\n")

    parser = argparse.ArgumentParser(description="Interactive agent chat tester")
    parser.add_argument("--project", default=None, help="Project name (default: agentcore-workshop)")
    parser.add_argument("--env", default=None, help="Environment (default: dev)")
    parser.add_argument("--component", default="orchestrator", help="Runtime component (default: orchestrator)")
    args = parser.parse_args()

    config = get_workshop_config(args.project, args.env)
    region = config["region"]

    print_section("Fetching Configuration")
    print(f"SSM prefix: {config['ssm_prefix']}")

    # Fetch from SSM: /{project}/{env}/auth/* and /{project}/{env}/runtimes/*
    user_pool_id = get_ssm_param("auth/user-pool-id", args.project, args.env)
    client_id = get_ssm_param("auth/app-client-id", args.project, args.env)
    runtime_arn = get_ssm_param(f"runtimes/{args.component}/arn", args.project, args.env)

    print_msg("Configuration fetched")
    print(f"  Runtime ARN: {runtime_arn}")
    print(f"  Region: {region}")

    # Authenticate
    print_section("Authentication")
    username = input("Enter username: ").strip()
    if not username:
        print_msg("Username is required", "error")
        sys.exit(1)
    password = getpass.getpass(f"Enter password for {username}: ")

    access_token, _, _ = authenticate_cognito(user_pool_id, client_id, username, password)

    # Chat loop
    session_id = generate_session_id()
    endpoint = f"https://bedrock-agentcore.{region}.amazonaws.com"
    escaped_arn = requests.utils.quote(runtime_arn, safe="")
    url = f"{endpoint}/runtimes/{escaped_arn}/invocations?qualifier=DEFAULT"

    print_section("Interactive Agent Chat")
    print(f"Session ID: {session_id}")
    print(f"\n{Fore.YELLOW}💡 Type 'exit' or 'quit' to end{Style.RESET_ALL}\n")

    def signal_handler(sig, frame):
        print(f"\n\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    while True:
        try:
            prompt = input(f"{Fore.CYAN}You:{Style.RESET_ALL} ").strip()
            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit"):
                print(f"\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}")
                break

            start_time = time.time()
            headers = {
                "Authorization": f"Bearer {access_token}",
                "X-Amzn-Trace-Id": generate_trace_id(),
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            }
            invoke_agent(url, prompt, session_id, headers)
            print(f"\n{Fore.CYAN}[Completed in {time.time() - start_time:.2f}s]{Style.RESET_ALL}\n")

        except (KeyboardInterrupt, EOFError):
            print(f"\n\n{Fore.GREEN}Goodbye!{Style.RESET_ALL}")
            break


if __name__ == "__main__":
    main()
