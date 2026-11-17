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

    def put_item(self, Item, ConditionExpression=None):
        if Item["op_id"] in self.items:
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
        and item["args"]["tags"] == ["factory:pm"]
    )
    assert b.ops[-1].get("duplicate")


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
