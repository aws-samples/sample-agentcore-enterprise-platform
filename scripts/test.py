#!/usr/bin/env python3
"""End-to-end test: get JWT token from Cognito, invoke orchestrator agent."""

import base64
import json
import os
import sys
import urllib.parse
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROJECT = os.environ.get("PROJECT_NAME", "agentcore-workshop")
ENV = os.environ.get("ENVIRONMENT", "dev")
# Resolved at runtime — the Cognito domain prefix includes the account ID
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def get_ssm(name):
    return boto3.client("ssm", region_name=REGION).get_parameter(
        Name=f"/{PROJECT}/{ENV}/{name}"
    )["Parameter"]["Value"]


def get_jwt_token():
    """Get M2M token via client_credentials grant."""
    pool_id = get_ssm("auth/user-pool-id")
    m2m_id = get_ssm("auth/m2m-client-id")
    secret = boto3.client("cognito-idp", region_name=REGION).describe_user_pool_client(
        UserPoolId=pool_id, ClientId=m2m_id
    )["UserPoolClient"]["ClientSecret"]

    auth = base64.b64encode(f"{m2m_id}:{secret}".encode()).decode()
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": "agentcore/invoke",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://{PROJECT}-{ENV}-{ACCOUNT}.auth.{REGION}.amazoncognito.com/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {auth}",
        },
    )
    if not req.full_url.startswith("https://"):
        raise ValueError("Token endpoint must use HTTPS")
    resp = json.loads(urllib.request.urlopen(req).read())  # nosec B310 — HTTPS validated above
    return resp["access_token"]


def invoke_runtime(token, prompt):
    """Invoke AgentCore Runtime via data plane with JWT."""
    runtime_arn = get_ssm("runtimes/orchestrator/arn")
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    # For CUSTOM_JWT, we inject the bearer token via event hook
    def inject_token(request, **kwargs):
        request.headers["X-Authorization"] = f"Bearer {token}"

    client.meta.events.register(
        "before-send.bedrock-agentcore.InvokeAgentRuntime", inject_token
    )

    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        payload=json.dumps({"prompt": prompt}).encode(),
    )
    return response["body"].read().decode()


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hello in one word."

    print("1. Getting JWT token from Cognito...")
    token = get_jwt_token()
    print(f"   ✓ Token: {token[:20]}...{token[-10:]}")

    print(f'\n2. Invoking orchestrator: "{prompt}"')
    try:
        result = invoke_runtime(token, prompt)
        print(f"   ✓ Response: {result[:500]}")
    except Exception as e:  # noqa: BLE001 — demo script: report any invoke failure and show the CLI fallback
        print(f"   ✗ {type(e).__name__}: {e}")
        print("\n   This is expected for CUSTOM_JWT runtimes via boto3.")
        print("   Use the agentcore CLI instead:")
        print(f'   npx @aws/agentcore invoke "{prompt}" \\')
        print("     --runtime agentcore_workshop_dev_orchestrator \\")
        print(f'     --bearer-token "{token[:20]}..."')

    print("\n3. Verifying all resources are healthy...")
    ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Runtimes
    runtimes = ctrl.list_agent_runtimes()
    for r in runtimes.get("agentRuntimes", []):
        if "agentcore_workshop_dev" in r["agentRuntimeName"]:
            status = "✓" if r["status"] == "READY" else "✗"
            print(f"   {status} Runtime: {r['agentRuntimeName']} → {r['status']}")

    # Gateway
    gateways = ctrl.list_gateways()
    for g in gateways.get("items", []):
        if "agentcore-workshop" in g["name"]:
            status = "✓" if g["status"] == "READY" else "✗"
            print(f"   {status} Gateway: {g['name']} → {g['status']}")

    # Memory
    memories = ctrl.list_memories()
    for m in memories.get("memories", []):
        if "agentcore_workshop_dev" in m["id"]:
            status = "✓" if m["status"] == "ACTIVE" else "✗"
            print(f"   {status} Memory:  {m['id']} → {m['status']}")

    # SSM params
    ssm = boto3.client("ssm", region_name=REGION)
    params = ssm.get_parameters_by_path(Path=f"/{PROJECT}/{ENV}", Recursive=True)
    print(f"   ✓ SSM Parameters: {len(params['Parameters'])} published")

    print("\nDone.")


if __name__ == "__main__":
    main()
