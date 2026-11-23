"""Nothing outside the account can deploy, and the exec role never gets administrator.

Run: python -m pytest factory/infra/tests -q
"""

import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cicd_stack import SoftwareFactoryCicdStack

ACCOUNT = "045129524125"
REGION = "us-east-1"


def _synth(**kwargs) -> dict:
    app = cdk.App()
    stack = SoftwareFactoryCicdStack(
        app,
        "SoftwareFactoryCicd",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
        synthesizer=cdk.CliCredentialsStackSynthesizer(),
        **kwargs,
    )
    return app.synth().get_stack_artifact(stack.artifact_id).template


@pytest.fixture(scope="module")
def template() -> dict:
    return _synth()


def _roles(template: dict) -> dict:
    out = {}
    for res in template["Resources"].values():
        if res["Type"] == "AWS::IAM::Role" and res["Properties"].get("RoleName"):
            out[res["Properties"]["RoleName"]] = res["Properties"]
    return out


def _statements(template: dict, role_name: str) -> list[dict]:
    """Every statement in every inline policy attached to the role."""
    logical = next(
        key
        for key, res in template["Resources"].items()
        if res["Type"] == "AWS::IAM::Role"
        and res["Properties"].get("RoleName") == role_name
    )
    found = []
    for res in template["Resources"].values():
        if res["Type"] != "AWS::IAM::Policy":
            continue
        if any(
            r.get("Ref") == logical
            for r in res["Properties"].get("Roles", [])
            if isinstance(r, dict)
        ):
            found += res["Properties"]["PolicyDocument"]["Statement"]
    return found


def test_no_role_can_assume_the_bootstrap_roles(template):
    """The bootstrap cfn-exec role carries AdministratorAccess. Nothing here may route to it."""
    blob = json.dumps(template)
    assert "cdk-hnb659fds" not in blob or "assets" in blob
    for res in template["Resources"].values():
        if res["Type"] != "AWS::IAM::Policy":
            continue
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if "sts:AssumeRole" in actions:
                pytest.fail(f"sts:AssumeRole granted on {stmt.get('Resource')}")


def test_nothing_attaches_administrator_access(template):
    blob = json.dumps(template)
    assert "AdministratorAccess" not in blob
    assert "PowerUserAccess" not in blob
    for props in _roles(template).values():
        assert not props.get("ManagedPolicyArns"), props.get("RoleName")


def test_exec_role_is_only_assumable_by_cloudformation(template):
    trust = _roles(template)["software-factory-cdk-exec"]["AssumeRolePolicyDocument"]
    assert len(trust["Statement"]) == 1
    stmt = trust["Statement"][0]
    assert stmt["Principal"] == {"Service": "cloudformation.amazonaws.com"}
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == ACCOUNT


def test_exec_role_may_only_attach_the_lambda_basic_execution_policy(template):
    attach = [
        s
        for s in _statements(template, "software-factory-cdk-exec")
        if "iam:AttachRolePolicy"
        in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert len(attach) == 1
    allowed = attach[0]["Condition"]["ArnEquals"]["iam:PolicyARN"]
    assert allowed == [
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    ]


def test_exec_role_iam_reach_is_limited_to_factory_named_roles(template):
    for stmt in _statements(template, "software-factory-cdk-exec"):
        actions = stmt["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        if not any(a.startswith("iam:") for a in actions):
            continue
        resources = stmt["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        for arn in resources:
            assert arn != "*", actions
            tail = arn.rsplit("/", 1)[-1]
            assert tail.startswith(
                ("software-factory-", "software_factory", "SoftwareFactory")
            ), arn
        # No action that mints a standalone identity or a long-lived credential.
        for action in actions:
            assert not action.startswith(
                (
                    "iam:CreateUser",
                    "iam:CreateAccessKey",
                    "iam:CreatePolicy",
                    "iam:CreateOpenIDConnectProvider",
                    "iam:CreateSAMLProvider",
                    "iam:PutRolePermissionsBoundary",
                )
            ), action


def test_exec_role_arns_use_each_service_s_own_separator(template):
    """arn:aws:lambda:...:function:NAME has a colon; a slash there matches no function at all,
    which is how the first pipeline deploy failed on lambda:UpdateFunctionCode."""
    arns = []
    for stmt in _statements(template, "software-factory-cdk-exec"):
        res = stmt["Resource"]
        arns += [
            r for r in (res if isinstance(res, list) else [res]) if isinstance(r, str)
        ]
    lam = [a for a in arns if a.startswith("arn:aws:lambda:") and ":function" in a]
    assert lam and all(":function:" in a for a in lam), lam
    assert not any(":function/" in a for a in arns)


def test_every_unscoped_statement_is_one_we_argued_for(template):
    """A new Resource:'*' has to be added here on purpose, not slip in."""
    allowed = {("software-factory-cdk-exec", "kms:CreateAlias")}
    seen = set()
    for role in ("software-factory-cdk-exec",):
        for stmt in _statements(template, role):
            resources = stmt["Resource"]
            resources = resources if isinstance(resources, list) else [resources]
            if "*" not in resources:
                continue
            actions = stmt["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            seen.add((role, min(actions)))
    assert seen == allowed, seen


def test_one_role_and_no_identity_provider(template):
    types = [r["Type"] for r in template["Resources"].values()]
    assert types.count("AWS::IAM::Role") == 1
    assert not any("OIDC" in t or "SAML" in t for t in types)
    assert list(_roles(template)) == ["software-factory-cdk-exec"]


def test_no_principal_outside_the_account_is_trusted(template):
    """No web identity, no federated or external principal: only CloudFormation, here."""
    for res in template["Resources"].values():
        if res["Type"] != "AWS::IAM::Role":
            continue
        for stmt in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
            assert stmt["Action"] == "sts:AssumeRole"
            assert stmt["Principal"] == {"Service": "cloudformation.amazonaws.com"}
            assert stmt["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
