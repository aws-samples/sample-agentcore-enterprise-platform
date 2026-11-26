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
"""

import ast
import sys
from pathlib import Path

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


def test_runtime_role_cannot_mint_tokens_for_arbitrary_users():
    """ForUserId takes an unverified user id: an impersonation path (Bug H8)."""
    for statement in _policy_statements():
        assert (
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
            not in statement["actions"]
        ), f"{statement['sid']} grants GetWorkloadAccessTokenForUserId"
    assert 'GetWorkloadAccessTokenForUserId"' not in RUNTIME_STACK.read_text()


def test_workload_identity_actions_are_never_on_star():
    offenders = []
    for statement in _policy_statements():
        if statement["effect"] == "DENY":
            continue
        if "*" not in statement["resources"]:
            continue
        for action in statement["actions"]:
            if action in WORKLOAD_IDENTITY_ACTIONS:
                offenders.append(f"{statement['sid']}: {action}")
    assert not offenders, f"workload-identity actions on '*': {offenders}"


def test_workload_identity_statement_is_scoped_to_own_identity():
    statements = {s["sid"]: s for s in _policy_statements()}
    wid = statements["AgentCoreWorkloadIdentity"]
    assert "*" not in wid["resources"]
    assert all(a in WORKLOAD_IDENTITY_ACTIONS for a in wid["actions"])
    broad = statements["AgentCoreAccess"]["actions"]
    assert not any(a in WORKLOAD_IDENTITY_ACTIONS for a in broad)
    source = RUNTIME_STACK.read_text()
    assert "default/workload-identity/{workload_identity_name}*" in source
    assert "workload_identity_name=rt_name" in source


def test_ssm_reads_are_scoped_to_the_project_path():
    source = RUNTIME_STACK.read_text()
    assert "arn:aws:ssm:*:*:parameter/{project_name}/*" in source, (
        "SSM reads must be scoped to this project's parameter path"
    )
