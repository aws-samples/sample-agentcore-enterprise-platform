"""Asana outbox/inbox, PM board rules and developer path limits (no AWS, no model)."""

from datetime import UTC, datetime, timedelta

import pytest
from factory.developer import allowed_paths, new_failures
from factory.pm import plan_board

from factory import asana


class FakeTable:
    class _Err(Exception):
        pass

    def __init__(self, item=None):
        self.items, self.item = {}, item
        self.meta = type(
            "M",
            (),
            {
                "client": type(
                    "C",
                    (),
                    {
                        "exceptions": type(
                            "E", (), {"ConditionalCheckFailedException": self._Err}
                        )
                    },
                )
            },
        )

    def put_item(self, Item, **condition):
        if (
            Item["op_id"] in self.items
            and self.items[Item["op_id"]]["status"] != "superseded"
        ):
            raise self._Err()
        self.items[Item["op_id"]] = Item

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}


def board(snapshot=None):
    b = asana.Board(None, "product_manager", "run-1")
    b.ops_table, b.snap_table = FakeTable(), FakeTable(snapshot)
    return b


def test_create_task_is_idempotent_across_runs_and_marked():
    a, b = board(), board()
    b.run_id = "run-2"
    b.ops_table = a.ops_table
    k1 = a.create_task("Ready for agent", "Add  X test", "why", ["tests/x.py"])
    k2 = b.create_task("Ready for agent", "add x test", "why", ["tests/x.py"])
    assert k1 == k2 and k1.startswith("factory:pm:") and len(a.ops_table.items) == 1
    item = a.ops_table.items[k1]
    assert (
        item["status"] == "pending"
        and "[factory:pm]" in item["args"]["notes"]
        and "tags" not in item["args"]
    )
    assert b.ops[-1].get("duplicate")


def test_dry_run_ops_are_marked_for_the_sync_to_ignore():
    b = board()
    b.op_status = "dry_run"
    b.move("123", "In review", "would open PR")
    assert {i["status"] for i in b.ops_table.items.values()} == {"dry_run"}


def test_move_to_done_also_completes_and_comments():
    b = board()
    b.move("123", "Done", "Merged: url")
    assert [o["op"] for o in b.ops] == ["move_task", "update_task", "comment"]


def test_stale_or_missing_snapshot_is_refused():
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    with pytest.raises(asana.StaleSnapshot):
        board().tasks(require_fresh=True)
    with pytest.raises(asana.StaleSnapshot):
        board({"project": "p", "snapshot_at": old, "tasks": []}).tasks(
            require_fresh=True
        )
    fresh = {
        "project": "p",
        "snapshot_at": datetime.now(UTC).isoformat(),
        "sections": ["Backlog"],
        "tasks": [{"gid": "1", "name": "t", "section": "Ready for agent"}],
    }
    b = board(fresh)
    assert b.tasks(require_fresh=True)[0]["gid"] == "1"
    assert b.ensure_sections() == [
        s for s in asana.SECTIONS if s not in ("Backlog", "Ready for agent")
    ]


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


def test_superseded_op_can_be_queued_again():
    a = board()
    k = a.create_task("Backlog", "T", "why")
    a.ops_table.items[k]["status"] = "superseded"
    b = board()
    b.ops_table = a.ops_table
    b.create_task("Backlog", "T", "why")
    assert a.ops_table.items[k]["status"] == "pending" and not b.ops[-1].get(
        "duplicate"
    )


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


def test_developer_skips_tasks_with_an_open_pr_branch_or_unchanged_attempt():
    from factory.developer import next_task, taken_tasks

    now = datetime.now(UTC).isoformat()
    tasks = [
        {
            "gid": "1",
            "name": "has PR",
            "section": "Ready for agent",
            "modified_at": "2026-11-01T00:00:00Z",
        },
        {
            "gid": "2",
            "name": "has branch",
            "section": "Ready for agent",
            "modified_at": "2026-11-01T00:00:00Z",
        },
        {
            "gid": "3",
            "name": "tried",
            "section": "Ready for agent",
            "modified_at": "2026-11-01T00:00:00Z",
        },
        {
            "gid": "4",
            "name": "free",
            "section": "Ready for agent",
            "modified_at": "2026-11-01T00:00:00Z",
        },
    ]
    b = board({"project": "p", "snapshot_at": now, "tasks": tasks})
    taken = taken_tasks(
        [{"branch": "factory/task/1"}, {"branch": "factory/ci-fix/9"}],
        {"factory/task/2"},
    )
    assert taken == {"1", "2"}
    assert next_task(b, taken, {"3": "2026-11-17T10:00:00Z"})["gid"] == "4"
    # a task edited after the last attempt is eligible again
    assert next_task(b, taken, {"3": "2026-10-01T00:00:00Z"})["gid"] == "3"


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


def test_cloud_sync_applies_only_pending_real_ops_with_any_client():
    from factory.asana_sync import apply_pending

    class Client:
        def __init__(self):
            self.calls, self.n = [], 0

        def sections(self, project):
            return {"Backlog": "s1"}

        def create_section(self, project, name):
            self.n += 1
            self.calls.append(("section", name))
            return f"new{self.n}"

        def create_task(self, project, section_gid, name, notes):
            self.calls.append(("task", section_gid, name))
            return "t9"

        def move_task(self, task_gid, section_gid):
            self.calls.append(("move", task_gid, section_gid))

        def comment(self, task_gid, text):
            raise RuntimeError("Asana 403")

        def update_task(self, task_gid, fields):
            self.calls.append(("update", task_gid, fields))

    P = asana.PROJECT
    ops = [
        {
            "op_id": "a",
            "op": "ensure_section",
            "project": P,
            "status": "pending",
            "created_at": "1",
            "args": {"section": "Done"},
        },
        {
            "op_id": "b",
            "op": "create_task",
            "project": P,
            "status": "pending",
            "created_at": "2",
            "args": {"section": "Backlog", "name": "T", "notes": "n"},
        },
        {
            "op_id": "c",
            "op": "move_task",
            "project": P,
            "status": "dry_run",
            "created_at": "3",
            "args": {"task_gid": "1", "section": "Done"},
        },
        {
            "op_id": "d",
            "op": "comment",
            "project": P,
            "status": "pending",
            "created_at": "4",
            "args": {"task_gid": "1", "text": "x"},
        },
        {
            "op_id": "e",
            "op": "update_task",
            "project": "test-x",
            "status": "pending",
            "created_at": "5",
            "args": {"task_gid": "1", "fields": {"completed": True}},
        },
        {
            "op_id": "f",
            "op": "update_task",
            "project": P,
            "status": "pending",
            "created_at": "6",
            "args": {"task_gid": "1", "fields": {"completed": True, "assignee": "me"}},
        },
    ]
    c = Client()
    out = apply_pending(ops, c)
    assert [(o["op_id"], o["status"]) for o in out] == [
        ("a", "applied"),
        ("b", "applied"),
        ("d", "failed"),
        ("f", "applied"),
    ]
    assert ("task", "s1", "T") in c.calls and (
        "update",
        "1",
        {"completed": True},
    ) in c.calls
