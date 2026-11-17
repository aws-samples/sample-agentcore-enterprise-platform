"""Daily redrive (00:05 UTC): move messages the dispatcher's cost stop parked back to the main queue.

Only parked messages move: the dispatcher marks them with the message attribute `parked=daily-cost-stop`
(older ones only carry a "parked": "daily cost stop..." field in the body). Real failures that SQS moved
to the DLQ after max receives have neither, so they stay for a human to look at.
"""

import json
import os

import boto3

PARKED = "daily-cost-stop"


def is_parked(msg: dict) -> bool:
    attr = (msg.get("MessageAttributes") or {}).get("parked", {}).get("StringValue")
    if attr == PARKED:
        return True
    try:
        return str(json.loads(msg["Body"]).get("parked", "")).startswith(
            "daily cost stop"
        )
    except (ValueError, AttributeError):
        return False


def handler(event, context, sqs=None, max_rounds: int = 50):
    sqs = sqs or boto3.client("sqs")
    dlq, queue = os.environ["DLQ_URL"], os.environ["QUEUE_URL"]
    moved, kept, seen = [], set(), set()
    for _ in range(max_rounds):
        msgs = sqs.receive_message(
            QueueUrl=dlq,
            MaxNumberOfMessages=10,
            VisibilityTimeout=60,
            WaitTimeSeconds=1,
            AttributeNames=["MessageGroupId"],
            MessageAttributeNames=["All"],
        ).get("Messages", [])
        fresh = [m for m in msgs if m["MessageId"] not in seen]
        if not fresh:
            break
        for m in fresh:
            seen.add(m["MessageId"])
            if not is_parked(m):
                kept.add(m["MessageId"])
                # Hand it back at once so later receives (and humans) see it; it stays in the DLQ.
                sqs.change_message_visibility(
                    QueueUrl=dlq, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0
                )
                continue
            body = json.loads(m["Body"])
            body.pop("parked", None)
            sqs.send_message(
                QueueUrl=queue,
                MessageBody=json.dumps(body),
                MessageGroupId=m.get("Attributes", {}).get("MessageGroupId", "redrive"),
                MessageDeduplicationId=f"redrive-{m['MessageId']}",
            )
            sqs.delete_message(QueueUrl=dlq, ReceiptHandle=m["ReceiptHandle"])
            moved.append(m["MessageId"])
    out = {"redriven": len(moved), "kept_in_dlq": len(kept)}
    print(json.dumps(out))
    return out
