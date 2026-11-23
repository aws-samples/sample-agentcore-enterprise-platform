"""The GitHub trust actually constrains who gets in, and nothing gets administrator.

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
REPO = "aws-samples/sample-agentcore-enterprise-platform"
# The live repository has immutable OIDC subject claims on, so this is the real prefix its
# tokens carry, not `repo:<owner>/<name>`.
PREFIX = "repo:aws-samples@8931462/sample-agentcore-enterprise-platform@1330929050"


def _synth(**kwargs) -> dict:
    app = cdk.App()
    stack = SoftwareFactoryCicdStack(
        app,
        "SoftwareFactoryCicd",
        github_repo=REPO,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
        synthesizer=cdk.CliCredentialsStackSynthesizer(),
        **kwargs,
    )
    return app.synth().get_stack_artifact(stack.artifact_id).template


@pytest.fixture(scope="module")
def template() -> dict:
    return _synth(subject_prefix=PREFIX)


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


def test_three_roles_and_nothing_else(template):
    assert set(_roles(template)) == {
        "software-factory-gha-diff",
        "software-factory-gha-deploy",
        "software-factory-cdk-exec",
    }


def test_the_oidc_provider_is_a_native_resource_not_a_custom_one(template):
    """The L2 provider ships a Lambda holding iam:*OIDCProvider* on '*' forever."""
    types = {res["Type"] for res in template["Resources"].values()}
    assert "AWS::IAM::OIDCProvider" in types
    assert not any(t.startswith("Custom::") for t in types), types


def test_both_github_roles_pin_the_audience_and_the_exact_subject(template):
    for name in ("software-factory-gha-diff", "software-factory-gha-deploy"):
        trust = _roles(template)[name]["AssumeRolePolicyDocument"]["Statement"]
        assert len(trust) == 1, name
        cond = trust[0]["Condition"]
        # A wildcard subject would let `factory/main-v2-evil` or another repo in.
        assert "StringLike" not in cond, name
        eq = cond["StringEquals"]
        assert eq["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com", (
            name
        )
        subs = eq["token.actions.githubusercontent.com:sub"]
        subs = subs if isinstance(subs, list) else [subs]
        assert subs, name
        for sub in subs:
            assert sub.startswith(f"{PREFIX}:"), f"{name} trusts {sub}"
            assert "*" not in sub, f"{name} trusts {sub}"


def test_the_subject_prefix_is_the_immutable_one_this_repository_actually_sends(
    template,
):
    """A plain `repo:<owner>/<name>` prefix is refused by STS with no usable diagnostic."""
    for name in ("software-factory-gha-diff", "software-factory-gha-deploy"):
        subs = _roles(template)[name]["AssumeRolePolicyDocument"]["Statement"][0][
            "Condition"
        ]["StringEquals"]["token.actions.githubusercontent.com:sub"]
        for sub in subs if isinstance(subs, list) else [subs]:
            assert sub.startswith("repo:aws-samples@8931462/"), sub
            assert "/sample-agentcore-enterprise-platform@1330929050:" in sub, sub


def test_the_prefix_defaults_to_the_plain_form_when_not_given():
    """Another repository without immutable claims still gets a working trust."""
    template = _synth()
    subs = _roles(template)["software-factory-gha-deploy"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert subs == [f"repo:{REPO}:environment:deploy"]


def test_deploy_role_is_reachable_only_from_the_deploy_environment(template):
    trust = _roles(template)["software-factory-gha-deploy"]["AssumeRolePolicyDocument"]
    sub = trust["Statement"][0]["Condition"]["StringEquals"][
        "token.actions.githubusercontent.com:sub"
    ]
    assert sub == [f"{PREFIX}:environment:deploy"]


def test_diff_role_can_write_nothing(template):
    for stmt in _statements(template, "software-factory-gha-diff"):
        assert stmt["Effect"] == "Allow"
        actions = stmt["Action"]
        for action in actions if isinstance(actions, list) else [actions]:
            assert action.split(":", 1)[1].startswith(("Describe", "Get", "List")), (
                f"diff role may call {action}"
            )


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


def test_deploy_role_passes_only_the_exec_role_and_only_to_cloudformation(template):
    passes = [
        s
        for s in _statements(template, "software-factory-gha-deploy")
        if "iam:PassRole"
        in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert len(passes) == 1
    stmt = passes[0]
    assert (
        stmt["Condition"]["StringEquals"]["iam:PassedToService"]
        == "cloudformation.amazonaws.com"
    )
    # The resource is an Fn::GetAtt on the exec role, so resolve the logical id.
    exec_logical = next(
        key
        for key, res in template["Resources"].items()
        if res["Type"] == "AWS::IAM::Role"
        and res["Properties"].get("RoleName") == "software-factory-cdk-exec"
    )
    assert stmt["Resource"] == {"Fn::GetAtt": [exec_logical, "Arn"]}


def test_deploy_role_cloudformation_scope_is_the_two_stacks(template):
    cfn = [
        s
        for s in _statements(template, "software-factory-gha-deploy")
        if any(
            a.startswith("cloudformation:")
            for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        )
    ]
    writes = [s for s in cfn if s["Resource"] != "*"]
    assert writes, "no stack-scoped CloudFormation statement"
    for stmt in writes:
        for arn in (
            stmt["Resource"]
            if isinstance(stmt["Resource"], list)
            else [stmt["Resource"]]
        ):
            assert arn in (
                f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/SoftwareFactory/*",
                f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/SoftwareFactoryData/*",
            ), arn
    # The one '*' is GetTemplateSummary, which parses a template body the caller supplies.
    for stmt in (s for s in cfn if s["Resource"] == "*"):
        assert stmt["Action"] == "cloudformation:GetTemplateSummary"


def test_the_pipeline_cannot_deploy_the_stack_that_grants_it_access(template):
    arns = json.dumps(
        [
            s.get("Resource")
            for s in _statements(template, "software-factory-gha-deploy")
        ]
    )
    assert "stack/SoftwareFactoryCicd" not in arns


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
    allowed = {
        ("software-factory-gha-diff", "cloudformation:GetTemplateSummary"),
        ("software-factory-gha-deploy", "cloudformation:GetTemplateSummary"),
        ("software-factory-cdk-exec", "kms:CreateAlias"),
    }
    seen = set()
    for role in (
        "software-factory-gha-diff",
        "software-factory-gha-deploy",
        "software-factory-cdk-exec",
    ):
        for stmt in _statements(template, role):
            resources = stmt["Resource"]
            resources = resources if isinstance(resources, list) else [resources]
            if "*" not in resources:
                continue
            actions = stmt["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            seen.add((role, min(actions)))
    assert seen == allowed, seen
