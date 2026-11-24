"""Run rows -> chat messages: what is announced, what is not, and the throttle (no AWS)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas"))
import notifier

P = "100000000001"


def rec(name="INSERT", **row):
    img = {
        k: ({"N": str(v)} if isinstance(v, float) else {"S": str(v)})
        for k, v in row.items()
    }
    return {"eventName": name, "dynamodb": {"NewImage": img}}


@pytest.mark.parametrize(
    "row,head",
    [
        (
            {
                "agent": "designer",
                "outcome": "spec_written",
                "trigger": "design-900001",
            },
            "Spec ready",
        ),
        (
            {
                "agent": "developer",
                "outcome": "pr_opened",
                "pr_url": "https://github.com/o/r/pull/7",
            },
            "PR opened",
        ),
        ({"agent": "pr_reviewer", "outcome": "review_posted"}, "Review posted"),
        (
            {"agent": "ci_fixer", "outcome": "failed", "reason": "boom"},
            "ci_fixer run failed",
        ),
    ],
)
def test_runs_a_person_should_know_about_are_announced(row, head):
    text, key = notifier.message(notifier.row_of(rec(**row)), P)
    assert head in text and key == ""


def test_the_task_link_and_cost_are_included():
    row = {
        "agent": "designer",
        "outcome": "spec_written",
        "trigger": "design-900001",
        "est_cost_usd": 0.42,
    }
    text, _ = notifier.message(notifier.row_of(rec(**row)), P)
    assert f"https://app.asana.com/0/{P}/900001" in text and "cost $0.42" in text


@pytest.mark.parametrize(
    "outcome", ["skipped", "reviewed_dry_run", "skipped_duplicate"]
)
def test_routine_outcomes_are_quiet(outcome):
    assert (
        notifier.message(notifier.row_of(rec(agent="developer", outcome=outcome)), P)
        is None
    )


def test_a_stale_board_is_throttled_by_key():
    _, key = notifier.message(
        notifier.row_of(rec(agent="designer", outcome="skipped_stale_snapshot")), P
    )
    assert key == "notify#skipped_stale_snapshot"


def test_only_inserts_are_posted_and_only_to_the_chat_host(monkeypatch):
    posted = []
    monkeypatch.setattr(
        notifier, "_webhook", lambda sm: "https://hooks.slack.com/triggers/x"
    )
    monkeypatch.setattr(notifier, "post", lambda url, text: posted.append((url, text)))
    monkeypatch.setattr(notifier.boto3, "client", lambda n: None)
    monkeypatch.setattr(notifier.boto3, "resource", lambda n: None)
    event = {
        "Records": [
            rec(agent="merge_handler", outcome="merged_recorded"),
            rec("MODIFY", agent="merge_handler", outcome="merged_recorded"),
        ]
    }
    assert notifier.handler(event, None) == {"sent": 1}
    assert posted[0][0].startswith(notifier.WEBHOOK_PREFIX)


def test_a_placeholder_or_foreign_url_turns_notifications_off(monkeypatch):
    class Sm:
        def __init__(self, v):
            self.v = v

        def get_secret_value(self, SecretId):
            return {"SecretString": self.v}

    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    for value in ("none", "https://evil.example/hook"):
        notifier._secret.update(at=0.0, url="")
        assert notifier._webhook(Sm(value)) == ""
    notifier._secret.update(at=0.0, url="")
    assert notifier._webhook(Sm("https://hooks.slack.com/triggers/T/1/x")).startswith(
        notifier.WEBHOOK_PREFIX
    )


def test_post_sends_the_text_field():
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def opener(req, timeout):
        seen["body"] = json.loads(req.data)
        return R()

    notifier.post("https://hooks.slack.com/x", "hi", opener=opener)
    assert seen["body"] == {"text": "hi"}
