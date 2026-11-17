"""Dispatcher daily cost stop (no AWS): over $10 today, messages are parked in the DLQ, not run."""

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas"))
import dispatcher

NOW = datetime(2026, 11, 17, 18, 30, 5, tzinfo=UTC)


class Fake:
    def __init__(self, spent):
        self.spent, self.invoked, self.parked, self.query = spent, [], [], None

    def get_metric_data(self, **kw):
        self.query = kw
        return {"MetricDataResults": [{"Values": [self.spent]}]}

    def invoke_agent_runtime(self, **kw):
        self.invoked.append(kw)
        return {"response": io.BytesIO(b'{"accepted": 1}')}

    def send_message(self, **kw):
        self.parked.append(kw)


def run(monkeypatch, spent):
    fake = Fake(spent)
    monkeypatch.setattr(
        dispatcher,
        "_clients",
        {"cloudwatch": fake, "sqs": fake, "bedrock-agentcore": fake},
    )
    monkeypatch.setenv("RUNTIME_ARN", "arn:runtime")
    monkeypatch.setenv("DLQ_URL", "https://dlq")
    monkeypatch.setenv("DAILY_COST_STOP_USD", "10")
    record = {
        "messageId": "m-1",
        "body": json.dumps({"role": "developer"}),
        "attributes": {"MessageGroupId": "developer"},
    }
    out = dispatcher.handler({"Records": [record]}, None, now=NOW)
    return fake, out


def test_under_the_cap_the_run_starts(monkeypatch):
    fake, out = run(monkeypatch, 9.99)
    assert (
        len(fake.invoked) == 1 and not fake.parked and out == {"batchItemFailures": []}
    )
    # the whole UTC day in one bucket, starting at midnight
    assert fake.query["StartTime"] == NOW.replace(hour=0, minute=0, second=0)
    assert fake.query["MetricDataQueries"][0]["MetricStat"]["Period"] == 66660


def test_over_the_cap_the_message_is_parked_not_run(monkeypatch):
    fake, out = run(monkeypatch, 10.01)
    assert not fake.invoked and out == {"batchItemFailures": []}
    parked = fake.parked[0]
    assert (
        parked["QueueUrl"] == "https://dlq" and parked["MessageGroupId"] == "developer"
    )
    assert json.loads(parked["MessageBody"])["role"] == "developer"
    assert "daily cost stop" in json.loads(parked["MessageBody"])["parked"]


def test_parked_messages_carry_the_redrive_mark(monkeypatch):
    fake, _ = run(monkeypatch, 10.01)
    assert (
        fake.parked[0]["MessageAttributes"]["parked"]["StringValue"]
        == "daily-cost-stop"
    )


def test_redrive_moves_only_cost_stop_parked_messages(monkeypatch):
    import redrive

    def msg(i, body, attrs=None):
        return {
            "MessageId": i,
            "ReceiptHandle": f"rh-{i}",
            "Body": json.dumps(body),
            "Attributes": {"MessageGroupId": f"g-{i}"},
            "MessageAttributes": attrs or {},
        }

    dlq = [
        msg("a", {"role": "developer"}, {"parked": {"StringValue": "daily-cost-stop"}}),
        msg(
            "b", {"role": "pr_reviewer", "parked": "daily cost stop: $15.19 > $10.00"}
        ),  # older mark, body only
        msg("c", {"role": "ci_fixer"}),  # a real failure after 3 receives
    ]

    class SQS:
        def __init__(self):
            self.sent, self.deleted, self.released = [], [], []

        def receive_message(self, **kw):
            return {
                "Messages": [m for m in dlq if m["ReceiptHandle"] not in self.deleted]
            }

        def send_message(self, **kw):
            self.sent.append(kw)

        def delete_message(self, QueueUrl, ReceiptHandle):
            self.deleted.append(ReceiptHandle)

        def change_message_visibility(self, **kw):
            self.released.append(kw["ReceiptHandle"])

    monkeypatch.setenv("DLQ_URL", "https://dlq")
    monkeypatch.setenv("QUEUE_URL", "https://q")
    sqs = SQS()
    out = redrive.handler({}, None, sqs=sqs)
    assert out == {"redriven": 2, "kept_in_dlq": 1}
    assert sorted(sqs.deleted) == ["rh-a", "rh-b"] and sqs.released == ["rh-c"]
    assert all(s["QueueUrl"] == "https://q" for s in sqs.sent)
    assert [json.loads(s["MessageBody"]) for s in sqs.sent] == [
        {"role": "developer"},
        {"role": "pr_reviewer"},
    ]
    assert (
        sqs.sent[1]["MessageGroupId"] == "g-b"
        and sqs.sent[1]["MessageDeduplicationId"] == "redrive-b"
    )
