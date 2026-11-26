"""Synthesized-template checks for the published, pinned guardrail version.

Skipped where aws-cdk-lib is not installed (the CI test job).
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Template

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.policy_loader import load_control
from stacks import runtime_stack as runtime_stack_module
from stacks.gateway_stack import GatewayStack
from stacks.runtime_stack import RuntimeStack


def _definition_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


@pytest.fixture(autouse=True)
def _repo_cwd(monkeypatch):
    # Asset paths such as tools/egress_interceptor are relative to the repo root.
    monkeypatch.chdir(REPO)


def _runtime_template() -> Template:
    app = cdk.App()
    stack = RuntimeStack(
        app,
        "rt",
        project_name="p",
        environment="prod",
        component_name="orchestrator",
        source_dir="agent-code/orchestrator",
        runtime_type="orchestrator",
        require_guardrails=True,
    )
    return Template.from_stack(stack)


def _gateway_template() -> Template:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "gw",
        project_name="p",
        environment="prod",
        cognito_issuer_url="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
        cognito_allowed_clients=["client"],
        enable_egress_filter=True,
    )
    return Template.from_stack(stack)


def _only_version(template: Template) -> tuple[str, dict]:
    versions = template.find_resources("AWS::Bedrock::GuardrailVersion")
    assert len(versions) == 1, "exactly one published guardrail version expected"
    return next(iter(versions.items()))


def test_runtime_receives_the_published_version():
    template = _runtime_template()
    logical_id, version = _only_version(template)
    expected = _definition_hash(load_control("guardrail.egress-default"))
    assert f"sha256:{expected}" in version["Properties"]["Description"]
    runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
    (runtime,) = runtimes.values()
    env = runtime["Properties"]["EnvironmentVariables"]
    assert env["GUARDRAIL_VERSION"] == {"Fn::GetAtt": [logical_id, "Version"]}
    assert "DRAFT" not in json.dumps(template.to_json())


def test_gateway_interceptor_receives_the_published_version():
    template = _gateway_template()
    logical_id, _ = _only_version(template)
    fns = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"FunctionName": "p-prod-egress-interceptor"}},
    )
    (fn,) = fns.values()
    env = fn["Properties"]["Environment"]["Variables"]
    assert env["GUARDRAIL_VERSION"] == {"Fn::GetAtt": [logical_id, "Version"]}
    assert "DRAFT" not in json.dumps(template.to_json())


def test_definition_change_changes_the_version_description(monkeypatch):
    _, before = _only_version(_runtime_template())
    changed = dict(load_control("guardrail.egress-default"))
    changed["BlockedInputMessaging"] = "changed"
    monkeypatch.setattr(runtime_stack_module, "load_control", lambda _id: changed)
    _, after = _only_version(_runtime_template())
    assert before["Properties"]["Description"] != after["Properties"]["Description"]
    assert os.path.isdir(REPO / "agent-code" / "orchestrator")
