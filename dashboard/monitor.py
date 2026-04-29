"""Workshop Monitor — polls AWS for deployment status and writes JSON for the dashboard."""
import json
import os
import time
import boto3
from datetime import datetime

PROJECT = os.environ.get("PROJECT_NAME", "agentcore-workshop")
ENV = os.environ.get("ENVIRONMENT", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = f"{PROJECT}-{ENV}"
OUTPUT = os.path.join(os.path.dirname(__file__), "public", "status.json")

# Stack → expected resources mapping
STACKS = {
    f"{PREFIX}-auth": {
        "module": "3/4", "team": "platform", "layer": "identity",
        "description": "Cognito User Pool + federated IdP + 3 OAuth clients",
        "checks": ["UserPool", "AppClient", "M2MClient", "SSM:issuer-url"],
    },
    f"{PREFIX}-identity": {
        "module": "4", "team": "platform", "layer": "identity",
        "description": "OAuth2 credential providers (Google, GitHub, Notion)",
        "checks": ["OAuth2Providers"],
    },
    f"{PREFIX}-memory": {
        "module": "A", "team": "agent", "layer": "service",
        "description": "AgentCore Memory + semantic/user_preference strategies",
        "checks": ["Memory", "SSM:memory-id"],
    },
    f"{PREFIX}-gateway": {
        "module": "5/7", "team": "platform", "layer": "service",
        "description": "MCP Gateway + CUSTOM_JWT auth + Lambda tool targets",
        "checks": ["Gateway", "GatewayRole", "SSM:gateway-url"],
    },
    f"{PREFIX}-runtime-orchestrator": {
        "module": "6", "team": "agent", "layer": "runtime",
        "description": "Orchestrator agent (HTTP) — ECR + CodeBuild + CfnRuntime",
        "checks": ["ECR", "CodeBuild", "Runtime", "SSM:runtime-arn"],
    },
    f"{PREFIX}-runtime-code-agent": {
        "module": "8", "team": "agent", "layer": "runtime",
        "description": "Code Agent (A2A protocol)",
        "checks": ["ECR", "CodeBuild", "Runtime"],
    },
    f"{PREFIX}-runtime-research-agent": {
        "module": "8", "team": "agent", "layer": "runtime",
        "description": "Research Agent (A2A protocol)",
        "checks": ["ECR", "CodeBuild", "Runtime"],
    },
    f"{PREFIX}-observability": {
        "module": "9", "team": "platform", "layer": "observability",
        "description": "CloudWatch vended logs + X-Ray per resource",
        "checks": ["LogDelivery", "XRayPolicy"],
    },
    f"{PREFIX}-networking": {
        "module": "3/C", "team": "platform", "layer": "foundation",
        "description": "VPC + subnets + VPC endpoints (optional)",
        "checks": ["VPC", "Subnets"],
    },
    f"{PREFIX}-security": {
        "module": "6a/S2", "team": "security", "layer": "foundation",
        "description": "KMS CMK + CloudTrail (optional)",
        "checks": ["KMSKey", "CloudTrail"],
    },
}

cfn = boto3.client("cloudformation", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)


def get_stack_status(stack_name: str) -> dict:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        stack = resp["Stacks"][0]
        status = stack["StackStatus"]
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        resources = []
        try:
            paginator = cfn.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=stack_name):
                for r in page["StackResourceSummaries"]:
                    resources.append({
                        "type": r["ResourceType"],
                        "logical": r["LogicalResourceId"],
                        "status": r["ResourceStatus"],
                        "physical": r.get("PhysicalResourceId", ""),
                    })
        except Exception:
            pass
        return {"status": status, "outputs": outputs, "resources": resources}
    except cfn.exceptions.ClientError:
        return {"status": "NOT_DEPLOYED", "outputs": {}, "resources": []}


def get_ssm_params() -> dict:
    params = {}
    try:
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=f"/{PROJECT}/{ENV}", Recursive=True):
            for p in page["Parameters"]:
                key = p["Name"].replace(f"/{PROJECT}/{ENV}/", "")
                params[key] = p["Value"]
    except Exception:
        pass
    return params


def poll():
    stacks_status = {}
    for stack_name, meta in STACKS.items():
        info = get_stack_status(stack_name)
        stacks_status[stack_name] = {**meta, **info}

    ssm_params = get_ssm_params()

    # Compute summary
    total = len(STACKS)
    deployed = sum(1 for s in stacks_status.values() if "COMPLETE" in s["status"] and "DELETE" not in s["status"])
    in_progress = sum(1 for s in stacks_status.values() if "IN_PROGRESS" in s["status"])
    failed = sum(1 for s in stacks_status.values() if "FAILED" in s["status"] or "ROLLBACK" in s["status"])
    not_deployed = total - deployed - in_progress - failed

    total_resources = sum(len(s["resources"]) for s in stacks_status.values())

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "project": PROJECT,
        "environment": ENV,
        "region": REGION,
        "account": boto3.client("sts").get_caller_identity()["Account"],
        "summary": {
            "total_stacks": total,
            "deployed": deployed,
            "in_progress": in_progress,
            "failed": failed,
            "not_deployed": not_deployed,
            "total_resources": total_resources,
        },
        "stacks": stacks_status,
        "ssm_parameters": ssm_params,
    }


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    print(f"Monitoring {PREFIX} in {REGION}... Writing to {OUTPUT}")
    while True:
        try:
            data = poll()
            with open(OUTPUT, "w") as f:
                json.dump(data, f, indent=2, default=str)
            d = data["summary"]
            print(f"[{data['timestamp']}] Deployed: {d['deployed']}/{d['total_stacks']} | "
                  f"In Progress: {d['in_progress']} | Failed: {d['failed']} | "
                  f"Resources: {d['total_resources']}")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(15)
