#!/usr/bin/env python3
# Adapted from fullstack-solution-template-for-agentcore
"""
Test AgentCore Gateway directly.

Reads configuration from SSM parameters using /{project}/{env}/ convention.

Usage:
    python scripts/test_gateway.py
    python scripts/test_gateway.py --project agentcore-workshop --env dev
"""

import argparse
import json
import os
import sys

import boto3
import requests

from utils import get_ssm_param, get_workshop_config, print_msg, print_section


def get_secret(secret_name: str) -> str:
    """Fetch secret from AWS Secrets Manager."""
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    client = boto3.client("secretsmanager", region_name=region)
    try:
        return client.get_secret_value(SecretId=secret_name)["SecretString"]
    except Exception as e:
        raise RuntimeError(f"Failed to retrieve secret {secret_name}: {e}")


def fetch_access_token(client_id: str, client_secret: str, token_url: str) -> str:
    """Fetch access token using client credentials flow."""
    response = requests.post(
        token_url,
        data=f"grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code != 200:
        print_msg(f"Token request failed: {response.status_code} - {response.text}", "error")
        sys.exit(1)
    return response.json()["access_token"]


def list_tools(gateway_url: str, access_token: str) -> dict:
    """List available tools via gateway."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    payload = {"jsonrpc": "2.0", "id": "list-tools-request", "method": "tools/list"}
    response = requests.post(gateway_url, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        print_msg(f"Gateway request failed: {response.status_code} - {response.text}", "error")
        sys.exit(1)
    return response.json()


def call_tool(gateway_url: str, access_token: str, tool_name: str, arguments: dict) -> dict:
    """Call a specific tool via gateway."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    payload = {
        "jsonrpc": "2.0",
        "id": "call-tool-request",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    response = requests.post(gateway_url, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        print_msg(f"Gateway request failed: {response.status_code} - {response.text}", "error")
        sys.exit(1)
    return response.json()


def main():
    parser = argparse.ArgumentParser(description="Test AgentCore Gateway")
    parser.add_argument("--project", default=None, help="Project name")
    parser.add_argument("--env", default=None, help="Environment")
    args = parser.parse_args()

    print_section("AgentCore Gateway Direct Test")

    config = get_workshop_config(args.project, args.env)
    prefix = config["ssm_prefix"]
    print(f"SSM prefix: {prefix}\n")

    # Fetch SSM parameters: /{project}/{env}/gateway/*, /{project}/{env}/auth/*
    print("Fetching configuration...")
    gateway_url = get_ssm_param("gateway/url", args.project, args.env)
    client_id = get_ssm_param("auth/machine-client-id", args.project, args.env)
    cognito_domain = get_ssm_param("auth/cognito-domain", args.project, args.env)
    token_url = f"https://{cognito_domain}/oauth2/token"

    # Get client secret from Secrets Manager
    client_secret = get_secret(f"{prefix}/machine-client-secret")
    print_msg("Configuration fetched")

    print(f"Gateway URL: {gateway_url}")
    print(f"Token URL: {token_url}")

    # Get access token
    print_section("Authentication")
    print("Fetching access token...")
    access_token = fetch_access_token(client_id, client_secret, token_url)
    print_msg("Access token obtained")

    # Test gateway
    print_section("Gateway Test")
    print("Calling tools/list...")
    tools = list_tools(gateway_url, access_token)
    print_msg("Gateway call successful")
    print("\nResponse:")
    print(json.dumps(tools, indent=2))

    # Call a tool
    print_section("Tool Call Test")
    tool_list = tools.get("result", {}).get("tools", [])
    if not tool_list:
        print_msg("No tools found in gateway", "error")
        sys.exit(1)

    # Use first available tool
    tool_name = tool_list[0]["name"]
    print(f"Calling tool: {tool_name}...")
    tool_result = call_tool(gateway_url, access_token, tool_name, {"text": "Hello world!", "N": 3})

    if "error" in tool_result:
        print_msg(f"Tool returned error: {tool_result['error']}", "error")
        sys.exit(1)

    print_msg("Tool call successful")
    print("\nResponse:")
    print(json.dumps(tool_result, indent=2))


if __name__ == "__main__":
    main()
