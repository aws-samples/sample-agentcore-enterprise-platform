"""Consult mode: history replay, cached grounding, and the 'queue these' gate (no AWS, no model)."""

from pathlib import Path

from factory import issues
from factory.consult import Consult


class Table:
    def __init__(self):
        self.items = []

    def query(self, **_):
        return {"Items": list(self.items)}

    def put_item(self, Item):
        self.items.append(Item)


def consult(message, table, replies):
    c = Consult.__new__(Consult)
    c.conversation_id, c.message, c.table, c.id, c.work = (
        "conv-0001",
        message,
        table,
        "pm-1",
        Path("/nonexistent"),
    )
    c.tracker = issues.Tracker("o/r", "", publish=False)
    c.calls = []
    c.gather_with_wt = lambda: (
        {"issues": [{"gid": "7", "title": "Old", "files": [], "completed": False}]},
        {"https://x/pull/2"},
        set(),
    )
    c.open_prs = []

    def chat(model, system, messages, max_tokens):
        c.calls.append((system, messages))
        return replies.pop(0)

    c.chat = chat
    return c


def test_second_turn_replays_history_and_reuses_grounding():
    table = Table()
    first = consult("What is done?", table, ["Answer one"]).execute()
    c = consult("And next?", table, ["Answer two"])
    c.gather_with_wt = lambda: (_ for _ in ()).throw(
        AssertionError("grounding gathered twice")
    )
    second = c.execute()
    system, messages = c.calls[0]
    assert first["turn"] == 1 and second["turn"] == 2
    assert [m["content"][0]["text"] for m in messages] == [
        "What is done?",
        "Answer one",
        "And next?",
    ]
    assert system[-1] == {"cachePoint": {"type": "default"}}
    assert "queued" not in second and not c.tracker.ops


def test_queue_these_plans_only_listed_changes_and_dry_runs_them():
    reply = '{"reconcile": [{"task_gid": "7", "action": "done", "pr_urls": ["https://x/pull/2"]}], "tasks": []}'
    c = consult("queue these: mark Old done (#2)", Table(), ["Sure.", reply])
    rec = c.execute()
    assert [(o["op"], o["issue"]) for o in c.tracker.ops] == [
        ("comment", 7),
        ("close", 7),
    ]
    assert not any(o["applied"] for o in c.tracker.ops) and rec["queued"] == 2
