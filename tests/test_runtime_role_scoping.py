"""The runtime role must not hand a compromised agent the whole account.

Regression guard for Checkov CKV_AWS_108 (data exfiltration) on the runtime
role's inline policy. Two statements were unscoped:

  - ECR pull on resources=["*"] — any runtime could pull every image in the
    account, and the repository ARN was already being passed into the function
    and ignored.
  - ssm:GetParameter on resources=["*"] — any parameter in the account, while
    infra_utils/agentcore_role.py (which calls itself the reference policy)
    scoped the same actions to the project path.

Parsed from source rather than synthesized: the CI test job has no aws-cdk-lib,
and this is the cheapest thing that fails if someone re-widens them.

The workload-identity checks (Bug H8) are synth tests instead: the scoped ARNs
are built with format_arn, which source parsing cannot evaluate. They skip when
aws-cdk-lib is not installed.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

RUNTIME_STACK = REPO / "stacks" / "runtime_stack.py"

# Actions that read data and DO support resource-level permissions, so a "*"
# here is a real finding rather than an AWS limitation.
MUST_BE_SCOPED = (
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ssm:GetParameter",
    "ssm:GetParameters",
)

# Actions AWS only accepts with "*". Kept explicit so the test documents which
# wildcards are deliberate instead of silently tolerating any of them.
WILDCARD_ALLOWED = {
    "ecr:GetAuthorizationToken",
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "cloudwatch:PutMetricData",
    # AgentCore data plane — scoping tracked separately, see the comment on the
    # AgentCoreAccess statement. Listed one by one (not a "bedrock-agentcore:"
    # prefix) so workload-identity actions can never ride along on "*".
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeGateway",
    "bedrock-agentcore:GetGateway",
    "bedrock-agentcore:ListGateways",
    "bedrock-agentcore:CreateEvent",
    "bedrock-agentcore:GetEvent",
    "bedrock-agentcore:ListEvents",
    "bedrock-agentcore:DeleteEvent",
    "bedrock-agentcore:ListSessions",
    "bedrock-agentcore:RetrieveMemoryRecords",
    "bedrock-agentcore:StartBrowserSession",
    "bedrock-agentcore:StopBrowserSession",
    "bedrock-agentcore:ConnectBrowserAutomationStream",
}

# Workload-identity / token-vault actions: must never be granted on "*".
WORKLOAD_IDENTITY_ACTIONS = (
    "bedrock-agentcore:GetResourceOauth2Token",
    "bedrock-agentcore:CreateWorkloadIdentity",
    "bedrock-agentcore:GetWorkloadAccessToken",
    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
)


def _policy_statements() -> list[dict]:
    """Every iam.PolicyStatement(...) in runtime_stack.py as {actions, resources}."""
    tree = ast.parse(RUNTIME_STACK.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None)
        if name != "PolicyStatement":
            continue
        entry = {"sid": None, "actions": [], "resources": [], "effect": "ALLOW"}
        for kw in node.keywords:
            if kw.arg in ("actions", "resources") and isinstance(kw.value, ast.List):
                entry[kw.arg] = [
                    el.value for el in kw.value.elts if isinstance(el, ast.Constant)
                ]
            elif kw.arg == "sid" and isinstance(kw.value, ast.Constant):
                entry["sid"] = kw.value.value
            elif kw.arg == "effect" and isinstance(kw.value, ast.Attribute):
                entry["effect"] = kw.value.attr  # iam.Effect.DENY → "DENY"
        found.append(entry)
    return found


def test_source_is_parseable_and_has_statements():
    statements = _policy_statements()
    assert len(statements) >= 6, f"expected the runtime role policies, got {statements}"


def test_readable_data_actions_are_never_on_star():
    offenders = []
    for statement in _policy_statements():
        # A Deny on "*" is the point, not a scoping miss — a deny that does not
        # cover everything is a bypass (DenyUngovernedInference).
        if statement["effect"] == "DENY":
            continue
        if "*" not in statement["resources"]:
            continue
        for action in statement["actions"]:
            if action in MUST_BE_SCOPED:
                offenders.append(f"{statement['sid']}: {action}")
    assert not offenders, (
        "these actions support resource-level permissions and must not use "
        f'resources=["*"]: {offenders}'
    )


def test_every_remaining_wildcard_is_a_known_aws_limitation():
    """A new unscoped statement should fail here rather than ship quietly."""
    unexplained = []
    for statement in _policy_statements():
        # Deny statements are exempt for the same reason as above: a Deny must
        # cover everything to enforce anything.
        if statement["effect"] == "DENY":
            continue
        if "*" not in statement["resources"]:
            continue
        for action in statement["actions"]:
            if action not in WILDCARD_ALLOWED:
                unexplained.append(f"{statement['sid']}: {action}")
    assert not unexplained, (
        "wildcard resource on actions that are not documented as requiring it: "
        f"{unexplained}. Scope them, or add them to WILDCARD_ALLOWED with a reason."
    )


def test_ecr_pull_is_scoped_to_the_repository_arn():
    """The repo ARN is a variable, so assert on intent, not on parsed literals.

    _policy_statements() only collects constants, so a variable resource shows up
    as an empty list. Checking for that emptiness would pass for the wrong reason
    — it has to be paired with 'the source actually references ecr_repo_arn'.
    """
    for statement in _policy_statements():
        if "ecr:BatchGetImage" not in statement["actions"]:
            continue
        assert "*" not in statement["resources"], "ECR pull is unscoped"
        source = RUNTIME_STACK.read_text()
        assert "resources=[ecr_repo_arn]" in source, (
            "ECR pull must use the repository ARN passed into the function"
        )
        return
    raise AssertionError("no ECR pull statement found")


def test_code_interpreter_session_actions_are_complete_and_scoped():
    """The shipped clients start, invoke, and optionally stop one system tool."""
    statements = {statement["sid"]: statement for statement in _policy_statements()}
    code_interpreter = statements["AgentCoreCodeInterpreter"]
    assert code_interpreter["actions"] == [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:StopCodeInterpreterSession",
    ]
    assert "*" not in code_interpreter["resources"]

    broad_actions = statements["AgentCoreAccess"]["actions"]
    assert not any("CodeInterpreter" in action for action in broad_actions)

    source = RUNTIME_STACK.read_text()
    assert 'account="aws"' in source
    assert 'resource="code-interpreter"' in source
    assert 'resource_name="aws.codeinterpreter.v1"' in source


RT_NAME = "proj_dev_orchestrator"


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _render(value) -> str:
    """Flatten a synthesized ARN (Fn::Join / Ref) into a comparable string."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_render(part) for part in parts)
    if isinstance(value, dict) and "Ref" in value:
        ref = value["Ref"]
        return f"${{{ref}}}"
    return json.dumps(value, sort_keys=True)


@pytest.fixture(scope="module")
def synth_statements() -> list[dict]:
    """Every statement of the synthesized runtime stack's IAM policies."""
    cdk = pytest.importorskip("aws_cdk")
    assertions = pytest.importorskip("aws_cdk.assertions")
    from stacks.runtime_stack import RuntimeStack

    app = cdk.App()
    stack = RuntimeStack(
        app,
        "RuntimeScopingTest",
        project_name="proj",
        environment="dev",
        component_name="orchestrator",
        source_dir="infra_utils",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    template = assertions.Template.from_stack(stack)
    statements = []
    for resource in template.find_resources("AWS::IAM::Policy").values():
        statements += resource["Properties"]["PolicyDocument"]["Statement"]
    return statements


def test_synth_runtime_role_cannot_mint_tokens_for_arbitrary_users(
    synth_statements,
):
    """ForUserId takes an unverified user id: an impersonation path (Bug H8)."""
    for statement in synth_statements:
        assert "bedrock-agentcore:GetWorkloadAccessTokenForUserId" not in _as_list(
            statement["Action"]
        ), f"{statement.get('Sid')} grants GetWorkloadAccessTokenForUserId"


def test_synth_workload_identity_actions_are_never_on_star(synth_statements):
    offenders = []
    for statement in synth_statements:
        if statement["Effect"] == "Deny":
            continue
        if "*" not in _as_list(statement["Resource"]):
            continue
        for action in _as_list(statement["Action"]):
            if action in WORKLOAD_IDENTITY_ACTIONS:
                offenders.append(f"{statement.get('Sid')}: {action}")
    assert not offenders, f"workload-identity actions on '*': {offenders}"


def test_synth_workload_identity_statement_is_scoped_to_own_identity(
    synth_statements,
):
    by_sid = {s.get("Sid"): s for s in synth_statements}
    wid = by_sid["AgentCoreWorkloadIdentity"]
    actions = _as_list(wid["Action"])
    resources = _as_list(wid["Resource"])
    assert "*" not in resources
    assert all(a in WORKLOAD_IDENTITY_ACTIONS for a in actions)
    assert "bedrock-agentcore:GetWorkloadAccessTokenForUserId" not in actions

    rendered = [_render(r) for r in resources]
    assert not any(r.endswith(":*") or r == "*" for r in rendered), rendered
    identity_arns = [r for r in rendered if "/workload-identity/" in r]
    assert identity_arns, rendered
    assert all(
        r.endswith(f"workload-identity-directory/default/workload-identity/{RT_NAME}-*")
        for r in identity_arns
    ), identity_arns

    broad = _as_list(by_sid["AgentCoreAccess"]["Action"])
    assert not any(a in WORKLOAD_IDENTITY_ACTIONS for a in broad)


def test_ssm_reads_are_scoped_to_the_project_path():
    source = RUNTIME_STACK.read_text()
    assert "arn:aws:ssm:*:*:parameter/{project_name}/*" in source, (
        "SSM reads must be scoped to this project's parameter path"
    )


def _synth_statements_for(runtime_type: str, component: str) -> list[dict]:
    cdk = pytest.importorskip("aws_cdk")
    assertions = pytest.importorskip("aws_cdk.assertions")
    from stacks.runtime_stack import RuntimeStack

    app = cdk.App()
    stack = RuntimeStack(
        app,
        f"RuntimeScoping{component.title().replace('-', '')}",
        project_name="proj",
        environment="dev",
        component_name=component,
        source_dir="infra_utils",
        runtime_type=runtime_type,
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    template = assertions.Template.from_stack(stack)
    statements = []
    for resource in template.find_resources("AWS::IAM::Policy").values():
        statements += resource["Properties"]["PolicyDocument"]["Statement"]
    return statements


def test_a2a_runtime_may_mint_tokens_only_for_sdk_local_identities():
    """A2A runtimes get no WorkloadAccessToken from the service (SigV4 inbound), so the
    SDK's local path creates a "workload-<hex>" identity and calls ForUserId on it.
    That grant must cover only SDK-created identities, never a runtime's own."""
    statements = _synth_statements_for("a2a_agent", "research-agent")
    sdk = [s for s in statements if s.get("Sid") == "AgentCoreSdkLocalIdentity"]
    assert len(sdk) == 1
    actions = set(_as_list(sdk[0]["Action"]))
    assert "bedrock-agentcore:GetWorkloadAccessTokenForUserId" in actions
    rendered = [_render(r) for r in _as_list(sdk[0]["Resource"])]
    identities = [r for r in rendered if "/workload-identity/" in r]
    assert identities and all(
        r.endswith("workload-identity-directory/default/workload-identity/workload-*")
        for r in identities
    ), identities
    for statement in statements:
        if statement.get("Sid") == "AgentCoreSdkLocalIdentity":
            continue
        assert "bedrock-agentcore:GetWorkloadAccessTokenForUserId" not in _as_list(
            statement["Action"]
        )


def test_http_runtime_has_no_sdk_local_identity_statement(synth_statements):
    assert not any(
        s.get("Sid") == "AgentCoreSdkLocalIdentity" for s in synth_statements
    )
