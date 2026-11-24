"""The planner Lambda: attempt times from run rows, one claim per human action (no AWS)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas"))
import planner as lam
from factory import config


class Runs:
    def __init__(self, rows):
        self.rows = rows

    def query(self, KeyConditionExpression, **kw):
        pk = KeyConditionExpression._values[1]
        return {"Items": [r for r in self.rows if r["pk"] == pk]}


class Dedupe:
    class meta:
        class client:
            class exceptions:
                class ConditionalCheckFailedException(Exception):
                    pass

    def __init__(self):
        self.keys = set()

    def put_item(self, Item, ConditionExpression):
        if Item["delivery_id"] in self.keys:
            raise self.meta.client.exceptions.ConditionalCheckFailedException()
        self.keys.add(Item["delivery_id"])


def test_attempt_times_come_from_finished_runs_by_task():
    runs = Runs(
        [
            {
                "pk": "agent#designer",
                "trigger": "design-11",
                "outcome": "spec_written",
                "started_at": "2026-11-24T10:00",
            },
            {
                "pk": "agent#designer",
                "trigger": "design-next",
                "outcome": "skipped",
                "started_at": "2026-11-24T11:00",
            },
            {
                "pk": "agent#developer",
                "trigger": "task-22",
                "outcome": "pr_opened",
                "started_at": "2026-11-24T12:00",
            },
        ]
    )
    got = lam.attempted(runs)
    assert got["designer"] == {"11": "2026-11-24T10:00"} and got["developer"] == {
        "22": "2026-11-24T12:00"
    }


def test_one_snapshot_write_plans_each_task_once(monkeypatch):
    monkeypatch.setattr(config, "TRACKER", "asana")
    monkeypatch.setattr(config, "ASANA_USERS", ["111111"])
    sent = []
    dedupe = Dedupe()

    class Sqs:
        def send_message(self, **kw):
            sent.append(kw)

    class Ddb:
        def Table(self, name):
            return dedupe if name == "dedupe" else Runs([])

    monkeypatch.setattr(lam.boto3, "resource", lambda n: Ddb())
    monkeypatch.setattr(lam.boto3, "client", lambda n: Sqs())
    monkeypatch.setenv("RUNS_TABLE", "runs")
    monkeypatch.setenv("DEDUPE_TABLE", "dedupe")
    monkeypatch.setenv("QUEUE_URL", "q")
    tasks = [
        {
            "gid": g,
            "name": "t",
            "section": s,
            "completed": False,
            "moved_by": "111111",
            "moved_at": "2026-11-24T10:00:00+00:00",
            "comments": [],
        }
        for g, s in (("1", "Needs design"), ("2", "Ready for agent"))
    ]
    event = {
        "Records": [{"dynamodb": {"NewImage": {"tasks": {"S": json.dumps(tasks)}}}}]
    }
    assert sorted(lam.handler(event, None)["planned"]) == ["designer:1", "developer:2"]
    assert (
        lam.handler(event, None)["planned"] == []
    )  # the same snapshot again: nothing new
    assert len({m["MessageGroupId"] for m in sent}) == 2
