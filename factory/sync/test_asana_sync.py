"""The workstation sync's own rules, against a fake board (no MCP, no AWS).

cd factory && .venv/bin/python -m pytest sync -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import asana_sync as s

from factory import asana

P = "proj"


class FakeBoard:
    project = P

    def __init__(self):
        self.calls = []
        self._sections = {name: f"s-{i}" for i, name in enumerate(asana.SECTIONS)}
        self._tasks = [{"gid": "900001", "section": asana.READY}]

    def sections(self):
        return dict(self._sections)

    def tasks(self):
        return list(self._tasks)

    def create_task(self, section_gid, name, notes):
        self.calls.append(("create", section_gid, name, notes))
        return "900099"

    def move_task(self, gid, section_gid):
        self.calls.append(("move", gid, section_gid))

    def comment(self, gid, text):
        self.calls.append(("comment", gid, text))

    def complete_task(self, gid):
        self.calls.append(("complete", gid))


def op(kind, i=0, **args):
    return {
        "op_id": f"o{i}",
        "op": kind,
        "project": P,
        "status": "pending",
        "created_at": f"2026-11-20T10:0{i}",
        "args": args,
    }


@pytest.mark.parametrize(
    "o,why",
    [
        (op("move_task", task_gid="900001", section=asana.READY), "only a person"),
        (
            op("move_task", task_gid="900001", section=asana.NEEDS_DESIGN),
            "only a person",
        ),
        (op("create_task", section=asana.READY, name="n", notes=""), "only a person"),
        (
            op("move_task", task_gid="900001", section="Platform Ops"),
            "not a factory section",
        ),
        (op("comment", task_gid="123", text=f"{asana.MARKER} x"), "not in project"),
        (op("comment", task_gid="900001", text="no marker"), "marker"),
        (op("delete_task", task_gid="900001"), "unknown op"),
        (
            {
                **op("comment", task_gid="900001", text=f"{asana.MARKER} x"),
                "project": "other",
            },
            "another project",
        ),
    ],
)
def test_the_sync_refuses_what_the_board_rules_forbid(o, why):
    assert why in s.refuse(o, P, {"900001"})


def test_pending_ops_apply_in_order_and_refusals_do_not_stop_the_rest():
    b = FakeBoard()
    ops = [
        op("comment", 2, task_gid="900001", text=f"{asana.MARKER} done"),
        op("move_task", 1, task_gid="900001", section=asana.READY),  # refused: a gate
        op("move_task", 0, task_gid="900001", section=asana.IN_PROGRESS),
        {
            **op("comment", 3, task_gid="900001", text=f"{asana.MARKER} dry"),
            "status": "dry_run",
        },
        op("create_task", 4, section=asana.DESIGN_REVIEW, name="New", notes="spec"),
        op("comment", 5, task_gid="900099", text=f"{asana.MARKER} on the new task"),
    ]
    out = s.apply_pending(ops, b)
    assert [(r["op_id"], r["status"]) for r in out] == [
        ("o0", "applied"),
        ("o1", "refused"),
        ("o2", "applied"),
        ("o4", "applied"),
        ("o5", "applied"),
    ]
    assert b.calls[0] == ("move", "900001", b._sections[asana.IN_PROGRESS])
    assert b.calls[2][3].endswith(
        asana.MARKER
    )  # a created task always carries the marker


def test_a_claimed_op_is_skipped():
    b = FakeBoard()
    out = s.apply_pending(
        [op("complete_task", task_gid="900001")], b, claim=lambda o: False
    )
    assert out == [] and b.calls == []


def test_moved_by_is_whoever_moved_the_task_into_its_current_section():
    t = {"gid": "1", "section": asana.READY, "moved_by": "creator", "moved_at": "x"}
    stories = [
        {
            "resource_subtype": "section_changed",
            "created_by": {"gid": "me"},
            "created_at": "2026-11-20T10:00:00.000Z",
            "text": 'Me moved this task from "Needs design" to "Design review" in P',
        },
        {
            "resource_subtype": "section_changed",
            "created_by": {"gid": "teammate"},
            "created_at": "2026-11-20T11:00:00.000Z",
            "text": 'Teammate moved this task from "Design review" to "Ready for agent" in P',
        },
        {
            "resource_subtype": "comment_added",
            "created_by": {"gid": "me"},
            "created_at": "2026-11-20T12:00:00.000Z",
            "text": "hi",
        },
    ]
    s.with_stories(t, stories)
    assert t["moved_by"] == "teammate" and t["moved_at"] == "2026-11-20T11:00:00+00:00"
    assert t["comments"] == [
        {"by": "me", "at": "2026-11-20T12:00:00+00:00", "text": "hi"}
    ]


def test_a_task_created_in_place_was_moved_there_by_its_creator():
    t = {"gid": "1", "section": asana.READY, "moved_by": "creator", "moved_at": "t0"}
    s.with_stories(
        t,
        [
            {
                "resource_subtype": "added_to_project",
                "created_by": {"gid": "creator"},
                "text": "added",
            }
        ],
    )
    assert t["moved_by"] == "creator"
