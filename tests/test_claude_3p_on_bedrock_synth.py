"""claude-3p-on-bedrock: synthesized-template checks for regressions found live.

Needs aws_cdk, so it is skipped in the CI pytest job (which has none); it runs
wherever the CDK app itself runs.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Template

REPO = Path(__file__).resolve().parents[1]
UC = REPO / "use-cases" / "claude-3p-on-bedrock"
FIXTURE = REPO / "tests" / "fixtures" / "contract" / "claude-3p-on-bedrock.yaml"


def _synth(extra: dict | None = None):
    import os

    # The fixture uses example values on purpose, like CI. Restored afterwards
    # so other test modules still see the platform's placeholder refusal.
    previous = os.environ.get("PLATFORM_ALLOW_PLACEHOLDERS")
    os.environ["PLATFORM_ALLOW_PLACEHOLDERS"] = "1"
    try:
        sys.path.insert(0, str(REPO))
        spec = importlib.util.spec_from_file_location("c3p_stack", UC / "stack.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        raw = yaml.safe_load(FIXTURE.read_text())["use_cases"]["claude-3p-on-bedrock"]
        raw.update(extra or {})
        app = cdk.App()
        env = cdk.Environment(account="111111111111", region="us-east-1")
        ctx = {
            "project": "p",
            "environment": "e",
            "prefix": "p-e",
            "ssm_prefix": "/p/e",
            "region": "us-east-1",
            "cdk_env": env,
        }
        mod.build(app, ctx, raw)
        return Template.from_stack(app.node.find_child("p-e-uc-claude-3p-on-bedrock"))
    finally:
        if previous is None:
            os.environ.pop("PLATFORM_ALLOW_PLACEHOLDERS", None)
        else:
            os.environ["PLATFORM_ALLOW_PLACEHOLDERS"] = previous


@pytest.fixture(scope="module")
def template():
    return _synth()


@pytest.fixture(scope="module")
def agentcore_template():
    return _synth(
        {"agentcore_tools": {"client_id": "22222222-2222-2222-2222-222222222222"}}
    )


def _gateway_env(t) -> dict:
    task = next(iter(t.find_resources("AWS::ECS::TaskDefinition").values()))
    gw = next(
        c for c in task["Properties"]["ContainerDefinitions"] if c["Name"] == "web"
    )
    return {e["Name"]: e["Value"] for e in gw.get("Environment", [])}


def test_agentcore_url_comes_from_ssm_at_deploy_time(agentcore_template, template):
    # Option (a): a CloudFormation SSM parameter, resolved at deploy, never a
    # synth-time lookup (which would break a first `deploy.sh build`).
    value = _gateway_env(agentcore_template)["AGENTCORE_GATEWAY_URL"]
    assert "Ref" in value
    params = agentcore_template.to_json()["Parameters"]
    assert params[value["Ref"]]["Default"] == "/p/e/gateway/url"
    assert "AGENTCORE_GATEWAY_URL" not in _gateway_env(template)


def test_agentcore_issuer_published_for_verify(agentcore_template, template):
    def names(t):
        return {
            r["Properties"]["Name"]
            for r in t.find_resources("AWS::SSM::Parameter").values()
        }

    key = "/p/e/use-cases/claude-3p-on-bedrock/agentcore-tools-issuer"
    assert key in names(agentcore_template)
    assert key not in names(template)


def test_build_role_can_check_existing_image_tags(template):
    # Without it the "image already exists" check reads AccessDenied as "not
    # found" and a rebuild pushes onto an immutable tag. CDK may emit Action as
    # a string or merge it into a list, so scan the statements directly.
    actions = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for st in policy["Properties"]["PolicyDocument"]["Statement"]:
            a = st["Action"]
            actions += a if isinstance(a, list) else [a]
    assert "ecr:DescribeImages" in actions


def test_service_waits_for_execution_role_policy(template):
    # Found live: the service was created while the execution role's policy was
    # still being created, and the first tasks failed with AccessDenied.
    policies = template.find_resources("AWS::IAM::Policy")
    exec_policy = next(k for k in policies if "ExecutionRole" in k)
    service = next(iter(template.find_resources("AWS::ECS::Service").values()))
    assert exec_policy in service.get("DependsOn", [])
