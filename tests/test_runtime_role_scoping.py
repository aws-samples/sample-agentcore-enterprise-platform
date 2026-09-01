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
    # AgentCore data/control plane — scoping tracked separately, see the comment
    # on the AgentCoreAccess statement.
    "bedrock-agentcore:",
}


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
            if not any(action.startswith(a) for a in WILDCARD_ALLOWED):
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


def test_ssm_reads_are_scoped_to_the_project_path():
    source = RUNTIME_STACK.read_text()
    assert "arn:aws:ssm:*:*:parameter/{project_name}/*" in source, (
        "SSM reads must be scoped to this project's parameter path"
    )
