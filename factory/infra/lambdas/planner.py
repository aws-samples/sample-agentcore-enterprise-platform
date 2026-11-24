"""Planner Lambda: a new board snapshot in, one queue message per task that needs an agent out.

Triggered by the snapshot table's stream. The rules live in agent-code/factory/planner.py; this
file only reads the runs table for attempt times, claims each job once in the dedupe table, and
sends it to the work queue.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-code"))

from factory import planner

CLAIM_TTL_S = 7 * 24 * 3600
_ATTEMPT_DONE = {
    "designer": {
        "spec_written",
        "spec_dry_run",
        "question_asked",
        "spec_rejected",
        "failed",
    },
    "developer": {
        "branch_ready_dry_run",
        "checks_failed",
        "needs_scoping",
        "pr_opened",
        "no_change",
        "revision_pushed",
        "revision_ready_dry_run",
        "revision_failed",
        "failed",
    },
}


def attempted(runs) -> dict[str, dict[str, str]]:
    """role -> task gid -> start of its latest finished attempt."""
    out: dict[str, dict[str, str]] = {}
    for role, prefix in (("designer", "design-"), ("developer", "task-")):
        seen: dict[str, str] = {}
        kwargs = {"KeyConditionExpression": Key("pk").eq(f"agent#{role}")}
        while True:
            page = runs.query(**kwargs)
            for i in page["Items"]:
                gid = str(i.get("trigger", "")).removeprefix(prefix).split("-")[0]
                if i.get("outcome") in _ATTEMPT_DONE[role] and i.get(
                    "started_at", ""
                ) > seen.get(gid, ""):
                    seen[gid] = i["started_at"]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        out[role] = seen
    return out


def claim(dedupe, key: str) -> bool:
    try:
        dedupe.put_item(
            Item={"delivery_id": key, "expires_at": int(time.time()) + CLAIM_TTL_S},
            ConditionExpression="attribute_not_exists(delivery_id)",
        )
        return True
    except dedupe.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def handler(event, context):
    ddb, sqs = boto3.resource("dynamodb"), boto3.client("sqs")
    runs, dedupe = (
        ddb.Table(os.environ["RUNS_TABLE"]),
        ddb.Table(os.environ["DEDUPE_TABLE"]),
    )
    queue = os.environ["QUEUE_URL"]
    sent = []
    for record in event.get("Records", []):
        image = (record.get("dynamodb") or {}).get("NewImage") or {}
        raw = (image.get("tasks") or {}).get("S")
        if not raw:
            continue
        tasks = json.loads(raw)
        for job in planner.jobs(tasks, attempted(runs)):
            msg = planner.message(job)
            if not claim(dedupe, msg["dedupe_key"]):
                continue  # this human action already started a run
            sqs.send_message(
                QueueUrl=queue,
                MessageBody=json.dumps(msg["body"]),
                MessageGroupId=msg["group"],
                MessageDeduplicationId=hashlib.sha256(
                    msg["dedupe_key"].encode()
                ).hexdigest(),
            )
            sent.append(f"{job['role']}:{job['gid']}")
    print(json.dumps({"planned": sent}))
    return {"planned": sent}
