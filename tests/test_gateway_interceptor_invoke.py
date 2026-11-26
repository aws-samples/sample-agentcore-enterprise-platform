"""The gateway role may invoke the egress interceptor, and nothing beyond the tools."""

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.gateway_stack import DEFAULT_TOOL_CONFIGS, GatewayStack

TOOL_ARN = "arn:aws:lambda:us-east-1:111111111111:function:p-dev-tool-*"


def _template(enable_egress_filter: bool) -> dict:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="p",
        environment="dev",
        cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x",
        cognito_allowed_clients=["client"],
        tool_configs=DEFAULT_TOOL_CONFIGS,
        enable_egress_filter=enable_egress_filter,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


def _logical_id(resources: dict, rtype: str, prop: str, value: str) -> str:
    for lid, res in resources.items():
        if res["Type"] == rtype and res.get("Properties", {}).get(prop) == value:
            return lid
    raise AssertionError(f"no {rtype} with {prop}={value}")


def _gateway_invoke_resources(template: dict) -> list:
    resources = template["Resources"]
    role_id = _logical_id(resources, "AWS::IAM::Role", "RoleName", "p-dev-gateway-role")
    found = []
    for res in resources.values():
        if res["Type"] != "AWS::IAM::Policy":
            continue
        if {"Ref": role_id} not in res["Properties"]["Roles"]:
            continue
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if "lambda:InvokeFunction" in actions:
                targets = stmt["Resource"]
                found += targets if isinstance(targets, list) else [targets]
    return found


def _refs(node, out: set):
    if isinstance(node, dict):
        if "Ref" in node and isinstance(node["Ref"], str):
            out.add(node["Ref"])
        if "Fn::GetAtt" in node:
            out.add(node["Fn::GetAtt"][0])
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def _assert_acyclic(template: dict):
    resources = template["Resources"]
    graph = {}
    for lid, res in resources.items():
        deps = set()
        _refs(res.get("Properties", {}), deps)
        depends = res.get("DependsOn", [])
        deps |= set([depends] if isinstance(depends, str) else depends)
        graph[lid] = {d for d in deps if d in resources}
    state = {}

    def visit(node):
        state[node] = "active"
        for dep in graph[node]:
            if state.get(dep) == "active":
                raise AssertionError(f"dependency cycle via {node} -> {dep}")
            if dep not in state:
                visit(dep)
        state[node] = "done"

    for lid in graph:
        if lid not in state:
            visit(lid)


def test_gateway_role_can_invoke_interceptor_only():
    template = _template(enable_egress_filter=True)
    fn_id = _logical_id(
        template["Resources"],
        "AWS::Lambda::Function",
        "FunctionName",
        "p-dev-egress-interceptor",
    )
    found = _gateway_invoke_resources(template)
    expected = [TOOL_ARN, {"Fn::GetAtt": [fn_id, "Arn"]}]
    assert sorted(json.dumps(r, sort_keys=True) for r in found) == sorted(
        json.dumps(r, sort_keys=True) for r in expected
    )
    _assert_acyclic(template)


def test_no_interceptor_grant_without_filter():
    assert _gateway_invoke_resources(_template(enable_egress_filter=False)) == [
        TOOL_ARN
    ]
