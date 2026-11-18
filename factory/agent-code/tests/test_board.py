"""Issue tracking, PM plan rules, developer path limits and the dedupe guards (no AWS, no model)."""

from factory import issues
from factory.developer import allowed_paths, new_failures
from factory.pm import plan_board


def test_plan_board_caps_ready_dedupes_and_validates_reconcile():
    tasks = [
        {
            "title": f"Task {i}",
            "why": "w",
            "acceptance": ["pytest passes"],
            "files": [f"a{i}.py"],
            "size": "S",
        }
        for i in range(7)
    ]
    tasks += [
        {"title": "Big", "why": "w", "acceptance": ["x"], "files": [], "size": "L"},
        {"title": "task 0", "why": "w", "acceptance": ["x"], "files": [], "size": "S"},
        {
            "title": "No criteria",
            "why": "w",
            "acceptance": [],
            "files": [],
            "size": "S",
        },
    ]
    proposal = {
        "tasks": tasks,
        "reconcile": [
            {"task_gid": "1", "action": "done", "pr_url": "https://x/pull/2"},
            {"task_gid": "1", "action": "done", "pr_url": "https://x/pull/999"},
            {"task_gid": "9", "action": "needs_scoping", "reason": "gone"},
        ],
    }
    reconcile, new, dropped = plan_board(
        proposal, [], {"1"}, {"https://x/pull/2"}, {"a0.py"}
    )
    sections = [t["section"] for t in new]
    assert sections.count("Ready for agent") == 5
    assert {t["title"] for t in new if t["section"] == "Backlog"} == {
        "Task 5",
        "Task 6",
        "Big",
        "No criteria",
    }
    assert len(reconcile) == 1 and reconcile[0]["section"] == "Done"
    assert len(dropped) == 3  # duplicate title, unmerged PR url, unknown gid
    assert new[1]["new_files"] == ["a1.py"]


def test_developer_paths_refuse_factory_workflows_and_escapes():
    ok, refused = allowed_paths(
        ["stacks/a.py", "factory/x.py", ".github/workflows/ci.yml", "../etc"],
        "add a test",
    )
    assert ok == {"stacks/a.py"} and len(refused) == 3
    ok, _ = allowed_paths([".github/workflows/ci.yml"], "fix the CI workflow")
    assert ok == {".github/workflows/ci.yml"}


def test_only_new_pytest_failures_count():
    base = "FAILED tests/a.py::x - boom\n"
    assert new_failures(base, base) == []
    assert new_failures(base + "FAILED tests/b.py::y\n", base) == ["tests/b.py::y"]


def test_acceptance_commands_are_allowlisted_and_shell_free():
    from factory.developer import acceptance_commands

    crit = [
        "`ruff check --select I001 .` exits 0",
        "`pytest tests/` passes",
        "`curl https://example.invalid` never",
        "`ruff check . ; curl x` no",
        "`ruff check --fix .` mutates",
    ]
    assert acceptance_commands(crit) == ["ruff check --select I001 .", "pytest tests/"]


def test_open_pr_coverage_comment_action_and_multi_pr_done():
    from factory.pm import covered_by_open_pr

    open_prs = [
        {"url": "https://x/pull/118", "files": ["migration/a.py", "tests/t.py"]}
    ]
    assert (
        covered_by_open_pr({"files": ["migration/a.py", "tests/new.py"]}, open_prs)
        == "https://x/pull/118"
    )
    assert (
        covered_by_open_pr(
            {"files": ["stacks/b.py", "stacks/c.py", "migration/a.py"]}, open_prs
        )
        == ""
    )
    proposal = {
        "reconcile": [
            {
                "task_gid": "1",
                "action": "done",
                "pr_urls": ["https://x/pull/82", "https://x/pull/88"],
            },
            {
                "task_gid": "2",
                "action": "comment",
                "pr_urls": ["https://x/pull/118"],
                "text": "PR open",
            },
            {
                "task_gid": "3",
                "action": "comment",
                "pr_urls": ["https://x/pull/7"],
                "text": "?",
            },
        ],
        "tasks": [
            {
                "title": "T",
                "why": "w",
                "acceptance": ["x"],
                "files": ["migration/a.py"],
                "size": "S",
            }
        ],
    }
    rec, new, dropped = plan_board(
        proposal,
        [],
        {"1", "2", "3"},
        {"https://x/pull/82", "https://x/pull/88"},
        set(),
        open_prs,
    )
    assert [(r["task"], r["section"]) for r in rec] == [("1", "Done"), ("2", None)]
    assert "pull/88" in rec[0]["comment"] and not new and len(dropped) == 2


def test_ruling_still_needs_merged_prs():
    rulings = [
        {"task_gid": "1", "action": "done", "pr_urls": ["https://x/pull/82"]},
        {"task_gid": "2", "action": "done", "pr_urls": ["https://x/pull/999"]},
    ]
    rec, _, dropped = plan_board(
        {"reconcile": rulings}, [], {"1", "2"}, {"https://x/pull/82"}, set()
    )
    assert [r["task"] for r in rec] == ["1"] and len(dropped) == 1


def test_ci_fixer_skips_a_failure_an_open_factory_pr_covers():
    from factory.ci_fixer import covering_pr

    prs = [
        {
            "number": 118,
            "title": "style: fix ruff I001 in 1 file(s)",
            "branch": "factory/ci-fix/1",
            "files": ["migration/simulation/existing-ec2-agent/agent.py"],
        },
        {
            "number": 119,
            "title": "ci: bump pinned aws-cdk CLI to 2.1148.1 in Secret Boundary workflow",
            "branch": "factory/ci-fix/2",
            "files": [".github/workflows/secret-boundary.yml"],
        },
    ]
    wf = ".github/workflows/secret-boundary.yml"
    assert (
        covering_pr(prs, "Secret Boundary", wf, {wf})["number"] == 119
    )  # same workflow file
    assert (
        covering_pr(prs, "Secret Boundary", "", set())["number"] == 119
    )  # same check, by title
    lint = ".github/workflows/python-lint.yml"
    assert (
        covering_pr(
            prs,
            "Python Code Quality",
            lint,
            {"migration/simulation/existing-ec2-agent/agent.py"},
        )["number"]
        == 118
    )  # same failing file
    assert covering_pr(prs, "Python Code Quality", lint, {"scripts/other.py"}) is None


def test_digest_sums_cost_by_role_and_lists_prs_and_failures():
    from factory.digest import summarize

    rows = {
        "ci_fixer": [
            {"outcome": "fix_pr_opened", "pr_url": "u1", "est_cost_usd": "0.05"},
            {"outcome": "skipped_duplicate", "est_cost_usd": 0},
        ],
        "pr_reviewer": [
            {
                "outcome": "failed",
                "trigger": "117",
                "reason": "boom",
                "est_cost_usd": 0.2,
            }
        ],
        "merge_handler": [
            {"outcome": "merged_recorded", "pr_url": "u0", "est_cost_usd": 0}
        ],
    }
    s = summarize(rows, "2026-11-17")
    assert (
        s["runs"] == 4
        and s["cost_usd_total"] == 0.25
        and s["cost_usd_by_role"]["ci_fixer"] == 0.05
    )
    assert (
        s["prs_opened"] == ["u1"]
        and s["prs_merged"] == ["u0"]
        and s["failures"][0]["trigger"] == "117"
    )
    assert s["outcomes_by_role"]["ci_fixer"] == {
        "fix_pr_opened": 1,
        "skipped_duplicate": 1,
    }


def issue(n, labels, created="2026-11-01T00:00:00Z", updated="2026-11-01T00:00:00Z"):
    return {
        "number": n,
        "title": f"t{n}",
        "body": "Files likely touched: a.py\n- `pytest tests/` passes",
        "labels": labels,
        "url": f"https://x/issues/{n}",
        "created_at": created,
        "updated_at": updated,
    }


def test_developer_takes_the_oldest_ready_issue_not_in_flight_or_tried():
    from factory.developer import next_task, taken_tasks

    ready = [
        issue(5, [issues.READY], created="2026-11-05T00:00:00Z"),
        issue(
            1, [issues.READY], created="2026-11-01T00:00:00Z"
        ),  # oldest, but has an open PR
        issue(2, [issues.READY, issues.IN_PROGRESS], created="2026-11-02T00:00:00Z"),
        issue(
            3, [issues.READY], created="2026-11-03T00:00:00Z"
        ),  # tried, unchanged since
        issue(4, [issues.READY], created="2026-11-04T00:00:00Z"),
    ]
    taken = taken_tasks(
        [{"branch": "factory/task/1"}, {"branch": "factory/ci-fix/9"}], set()
    )
    assert taken == {"1"}
    task = next_task(ready, taken, {"3": "2026-11-17T10:00:00Z"})
    assert (
        task["gid"] == "4"
        and task["files"] == ["a.py"]
        and task["acceptance"] == ["`pytest tests/` passes"]
    )
    assert (
        next_task(ready, taken, {"3": "2026-10-01T00:00:00Z"})["gid"] == "3"
    )  # edited after the last try


def test_tracker_dry_run_records_and_never_calls_github(monkeypatch):
    from factory import github

    calls = []
    monkeypatch.setattr(
        github, "request", lambda *a, **k: calls.append(a) or {"number": 99}
    )
    dry = issues.Tracker("o/r", "tok", publish=False)
    reconcile = [
        {"task": "7", "section": "Done", "comment": "Implemented by https://x/pull/2"},
        {"task": "8", "section": "Needs scoping", "comment": "target unsupported"},
        {"task": "9", "section": None, "comment": "see #12"},
    ]
    new = [
        {"title": "T", "body": "b", "section": "Ready for agent"},
        {"title": "U", "body": "b", "section": "Backlog"},
    ]
    issues.apply_plan(dry, reconcile, new)
    assert not calls and all(not o["applied"] for o in dry.ops)
    assert [o["op"] for o in dry.ops] == [
        "propose",
        "propose",
        "comment",
        "propose",
        "propose",
    ]
    # Closes and relabels wait for /approve: the plan alone never closes or relabels an issue.
    assert not {"close", "add_labels", "remove_label", "create"} & {
        o["op"] for o in dry.ops
    }
    assert [o["action"] for o in dry.ops if o["op"] == "propose"] == [
        "close",
        "needs_scoping",
        "create",
        "create",
    ]
    assert dry.ops[-2]["labels"] == [issues.PM, issues.READY] and dry.ops[-1][
        "labels"
    ] == [issues.PM, issues.BACKLOG]
    live = issues.Tracker("o/r", "tok", publish=True)
    assert live.create("T", "b", [issues.PM]) == 99
    method, path, _tok, body = calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues") and issues.MARKER in body[
        "body"
    ]
