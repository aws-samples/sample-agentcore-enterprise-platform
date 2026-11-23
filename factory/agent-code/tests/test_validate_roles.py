"""Every role the server dispatches on must have a validate() branch.

issue_command never had one, so the runtime answered 400 to every /pm, /dev, /approve and
/reject from the day the feature landed: validate() fell through to "unsupported role". The
root cause is that nothing tied the two lists together, so these tests do that. A role added
to _dispatch with no validate branch, or with no payload below, fails here.
"""

import ast
import pathlib

import pytest

from factory import server

SOURCE = pathlib.Path(server.__file__).read_text()

# One minimal payload per dispatched role, as its producer sends it. A new role must add one.
PAYLOADS = {
    "issue_command": {
        "role": "issue_command",
        "command": "dev",
        "args": "",
        "issue": {"number": 7},
        "actor": {"login": "Robobc"},
    },
    "pr_reviewer": {"role": "pr_reviewer", "pull_request": {"number": 7}},
    "product_manager": {"role": "product_manager"},
    "developer": {"role": "developer"},
    "designer": {"role": "designer"},
    "merge_handler": {"role": "merge_handler", "pull_request": {"number": 7}},
    "digest": {"role": "digest"},
    "ci_fixer": {
        "role": "ci_fixer",
        "workflow_run": {
            "id": 7,
            "path": ".github/workflows/python-lint.yml",
            "conclusion": "failure",
            "head_branch": "feature",
        },
    },
}


def dispatched_roles() -> set[str]:
    """The role strings _dispatch compares against, read from the source so a new one is seen."""
    fn = next(
        n
        for n in ast.walk(ast.parse(SOURCE))
        if isinstance(n, ast.FunctionDef) and n.name == "_dispatch"
    )
    roles = {
        c.comparators[0].value
        for c in ast.walk(fn)
        if isinstance(c, ast.Compare)
        and isinstance(c.left, ast.Name)
        and c.left.id == "role"  # not the outcome comparisons in the same function
        and isinstance(c.ops[0], ast.Eq)
        and isinstance(c.comparators[0], ast.Constant)
        and isinstance(c.comparators[0].value, str)
    }
    return roles | {"ci_fixer"}  # _dispatch's fallthrough path


def test_the_dispatched_roles_are_the_ones_we_think():
    assert dispatched_roles() == set(PAYLOADS), (
        "a role was added to _dispatch or removed; add or drop its payload above, "
        "and give it a validate() branch"
    )


@pytest.mark.parametrize("role", sorted(PAYLOADS))
def test_every_dispatched_role_has_a_validate_branch(role):
    """The bug: validate() raised 'unsupported role', so do_POST answered 400 and handle() never ran."""
    got = server.validate(
        dict(PAYLOADS[role], workflow_run=PAYLOADS[role].get("workflow_run", {}))
    )
    assert got.get("id") or got.get("name"), f"{role} has no validate() branch"


def test_an_unknown_role_is_still_refused():
    with pytest.raises(ValueError, match="unsupported role"):
        server.validate({"role": "rm_minus_rf", "workflow_run": {}})


# --- the issue_command branch's own checks -----------------------------------


def command(**over) -> dict:
    return {**PAYLOADS["issue_command"], **over, "workflow_run": {}}


def test_a_valid_command_names_the_issue_it_came_from():
    assert server.validate(command(command="pm", args="what next?"))["id"] == "pm-7"


@pytest.mark.parametrize(
    "payload,why",
    [
        (command(command="rm"), "command must be one of"),
        (
            command(command="/dev"),
            "command must be one of",
        ),  # the slash is stripped by ingress
        (command(issue={"number": 0}), "must be positive"),
        (command(issue={"number": -3}), "must be positive"),
        (command(actor={"login": "not a login!"}), "actor.login"),
        (command(actor={}), "actor.login"),
        (command(command="pm", args="   "), "needs a question"),
        (command(command="pm", args="x" * 4001), "at most 4000"),
        (command(command="pm", args=123), "at most 4000"),
        (command(command="approve", args="whatever"), "'all' or P-xxxxxx"),
        (command(command="approve", args="P-ZZZZZZ"), "'all' or P-xxxxxx"),
        (command(command="reject", args="all"), "reject proposals by id"),
        (
            command(repository={"full_name": "someone/else"}),
            "not from the configured repository",
        ),
    ],
)
def test_a_malformed_command_is_refused(payload, why):
    with pytest.raises(ValueError, match=why):
        server.validate(payload)


@pytest.mark.parametrize(
    "name,args",
    [
        ("pm", "a question"),
        ("dev", ""),
        ("ready", ""),
        ("approve", "all"),
        ("approve", "P-abc123 P-def456"),
        ("reject", "P-abc123"),
    ],
)
def test_the_commands_the_ingress_can_send_are_accepted(name, args):
    """What ingress._command produces must pass: otherwise the runtime 400s a real comment."""
    assert server.validate(command(command=name, args=args))["id"] == f"{name}-7"


def test_a_bot_login_is_accepted():
    """github-actions[bot] opens issues here; the author allowlist, not validate, refuses it."""
    assert server.validate(command(actor={"login": "github-actions[bot]"}))
