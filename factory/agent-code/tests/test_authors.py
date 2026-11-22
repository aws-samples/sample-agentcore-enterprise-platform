"""The factory acts only on issues opened by an allowlisted author.

Applies to the developer taking a task and the PM reconciling or commenting. Not to PR reviews,
the CI fixer or the merge handler: those judge code, not a request.
"""

import pytest

from factory import config, github, issues, pm, server

OTHER = "a-maintainer"


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_RUNS_TABLE", raising=False)
    monkeypatch.setattr(config, "ERRORS", [])
    monkeypatch.setattr(server, "preflight", lambda role: None)
    monkeypatch.setattr(server, "Path", lambda *a: tmp_path)
    monkeypatch.setattr(server, "_github_token", lambda: "ghs_x")


def issue(number: int, author: str, labels=(issues.READY,), body="") -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": body,
        "author": author,
        "labels": list(labels),
        "url": f"https://github.com/x/y/issues/{number}",
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
    }


# --- the list itself ----------------------------------------------------------


def test_the_user_is_allowed_and_anyone_else_is_not():
    assert config.author_allowed("Robobc")
    assert config.author_allowed("robobc")  # GitHub logins are case-insensitive
    assert not config.author_allowed(OTHER)
    assert not config.author_allowed("")


def test_an_empty_list_means_nobody_and_says_so(monkeypatch):
    monkeypatch.setitem(config.AUTHORS, config.REPO, [])
    assert config.author_allowed("Robobc") is False
    assert any("no author allowlist" in e for e in config.ERRORS)


def test_an_unknown_repo_means_nobody(monkeypatch):
    assert config.author_allowed("Robobc", repo="someone/other-repo") is False
    assert config.ERRORS


def test_a_teammate_is_a_config_edit_not_a_code_change(monkeypatch):
    monkeypatch.setitem(config.AUTHORS, config.REPO, ["Robobc", OTHER])
    assert config.author_allowed(OTHER) and config.author_allowed("Robobc")


# --- developer: the scheduled path -------------------------------------------


def _developer(monkeypatch, ready: list[dict]) -> dict:
    monkeypatch.setattr(issues, "open_issues", lambda *a, **k: ready)
    monkeypatch.setattr(github, "open_factory_prs", lambda *a: [])
    monkeypatch.setattr(github, "factory_task_branches", lambda *a: set())
    monkeypatch.setattr(server, "attempted_tasks", lambda s: {})
    monkeypatch.setattr(
        server.Developer,
        "execute",
        lambda self: pytest.fail("the developer ran on a disallowed issue"),
    )
    return server.handle_developer({})


def test_an_issue_from_another_author_is_skipped_with_the_reason(monkeypatch):
    rec = _developer(monkeypatch, [issue(7, OTHER)])
    assert rec["outcome"] == "skipped"
    assert rec["reason"] == f"author_not_allowed:{OTHER}"


def test_a_maintainers_ready_label_does_not_widen_the_allowlist(monkeypatch):
    """/dev and the factory:ready label both land here; the issue's own author still decides."""
    monkeypatch.setattr(
        github,
        "request",
        lambda m, p, t, *a: {
            "number": 7,
            "state": "open",
            "title": "t",
            "body": "",
            "labels": [{"name": issues.READY}],
            "user": {"login": OTHER},
        },
    )
    rec = server._take_issue(7, "ghs_x")
    assert rec == {"outcome": "skipped", "reason": f"author_not_allowed:{OTHER}"}


def test_the_users_own_issue_is_taken(monkeypatch):
    monkeypatch.setattr(issues, "open_issues", lambda *a, **k: [issue(7, "Robobc")])
    monkeypatch.setattr(github, "open_factory_prs", lambda *a: [])
    monkeypatch.setattr(github, "factory_task_branches", lambda *a: set())
    monkeypatch.setattr(server, "attempted_tasks", lambda s: {})
    taken = {}

    def execute(self):
        taken["gid"] = self.task["gid"]
        return {"outcome": "branch_ready_dry_run", "pr_title": "t"}

    monkeypatch.setattr(server.Developer, "execute", execute)
    monkeypatch.setattr(server.Developer, "move", lambda *a, **k: None)
    server.handle_developer({})
    assert taken["gid"] == "7"  # it reached the agent, which only allowed issues do


def test_an_empty_allowlist_stops_the_developer_entirely(monkeypatch):
    monkeypatch.setitem(config.AUTHORS, config.REPO, [])
    rec = _developer(monkeypatch, [issue(7, "Robobc")])
    assert rec["reason"] == "author_not_allowed:Robobc"


# --- PM: the board is what its writes are gated against ----------------------


def test_the_board_holds_only_allowlisted_and_factory_issues(monkeypatch):
    rows = [
        issue(1, "Robobc"),
        issue(2, OTHER),
        issue(3, "robobc-software-factory[bot]", labels=[issues.PM]),
        issue(4, OTHER, labels=[], body=f"x {issues.MARKER} y"),
        issue(5, OTHER, labels=["factory:digest"]),
    ]
    monkeypatch.setattr(issues, "open_issues", lambda *a, **k: rows)
    kept = [
        i["number"]
        for i in rows
        if config.author_allowed(i["author"])
        or issues.PM in i["labels"]
        or issues.MARKER in i["body"]
    ]
    assert kept == [1, 3, 4]  # not 2 (another author), not 5 (the digest issue)


def test_plan_board_cannot_touch_an_issue_absent_from_the_board():
    """Why filtering the board is enough: an op naming an off-board gid is dropped."""
    reconcile, _, dropped = pm.plan_board(
        {
            "reconcile": [
                {"task_gid": "2", "action": "comment", "text": "someone else's issue"}
            ]
        },
        [issue(1, "Robobc")],
        {"1"},
        set(),
        set(),
    )
    assert reconcile == []
    assert dropped[0]["why"] == "unknown task gid"


def test_pm_command_answers_only_on_an_allowlisted_issue(monkeypatch):
    monkeypatch.setattr(
        github, "request", lambda *a, **k: {"number": 9, "user": {"login": OTHER}}
    )
    monkeypatch.setattr(
        server, "Consult", lambda *a: pytest.fail("the PM answered on another's issue")
    )
    assert server._pm_comment(9, "what about this?", "ghs_x") == {
        "outcome": "skipped",
        "reason": f"author_not_allowed:{OTHER}",
    }


# --- reviews are not affected ------------------------------------------------


def test_a_pr_review_runs_whoever_opened_the_pr(monkeypatch):
    """The reviewer judges code, so the allowlist must not reach it."""
    pr = {
        "number": 42,
        "title": "a fix",
        "body": "",
        "user": {"login": OTHER},
        "head": {"sha": "a" * 40, "ref": "feature"},
        "base": {"sha": "b" * 40, "repo": {"full_name": server.REPO}},
    }

    def clone(work):  # the real one creates the work dir the run row is written into
        work.mkdir(parents=True, exist_ok=True)
        return work

    monkeypatch.setattr(github, "get_pull", lambda *a: pr)
    monkeypatch.setattr(server, "clone", clone)
    monkeypatch.setattr(server, "load_app_creds", lambda: None)
    ran = {}

    def execute(self):
        ran["author"] = self.pr["user"]["login"]
        return {"outcome": "reviewed_dry_run", "review": {}}

    monkeypatch.setattr(server.Review, "execute", execute)
    server.handle_review({"pull_request": {"number": 42}})
    assert ran["author"] == OTHER  # reviewed anyway
