"""SQS FIFO -> AgentCore Runtime. The runtime returns at once and works async (design 2.2).

Hard stop: once the day's summed RunCostUSD (UTC) passes DAILY_COST_STOP_USD, every new message is
parked in the DLQ instead of starting a run, until the sum resets at 00:00 UTC. Parked messages can
be redriven to the queue the next day.
"""

import json
import os
from datetime import UTC, datetime

import boto3

_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def spent_today(cw, now: datetime) -> float:
    """Sum of the account-wide RunCostUSD metric since 00:00 UTC (no dimension = every agent)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period = max(
        60, -(-int((now - midnight).total_seconds()) // 60) * 60
    )  # one bucket for the whole day
    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "cost",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "SoftwareFactory",
                        "MetricName": "RunCostUSD",
                    },
                    "Period": period,
                    "Stat": "Sum",
                },
            }
        ],
        StartTime=midnight,
        EndTime=now,
    )
    return float(sum(resp["MetricDataResults"][0]["Values"]))


def session_id(message: dict, record: dict) -> str:
    # Deterministic id: a retry reattaches to the same session instead of starting a second run.
    role = message.get("role")
    if role == "merge_handler":
        sid = f"factory-merge-{int(message['pull_request']['number'])}"
    elif role in (
        "product_manager",
        "developer",
        "designer",
        "digest",
        "issue_command",
    ):
        # Scheduled/manual: one session per SQS message (its id is unique per delivery).
        sid = f"factory-{role}-{record['messageId']}"[:100]
    elif role == "pr_reviewer":
        pr = message["pull_request"]
        sid = f"factory-rev-{int(pr['number'])}-{str(pr.get('head_sha', ''))[:12]}"
    else:
        run = message["workflow_run"]
        sid = f"factory-ci-{int(run['id'])}-attempt-{int(run.get('run_attempt') or 1)}"
    return sid.ljust(33, "-")


def handler(event, context, now: datetime | None = None):
    failures = []
    cap = float(os.environ.get("DAILY_COST_STOP_USD", "10"))
    spent = None
    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            if spent is None:
                spent = spent_today(_client("cloudwatch"), now or datetime.now(UTC))
            if spent > cap:
                _client("sqs").send_message(
                    QueueUrl=os.environ["DLQ_URL"],
                    MessageBody=json.dumps(
                        {
                            **message,
                            "parked": f"daily cost stop: ${spent:.2f} > ${cap:.2f}",
                        }
                    ),
                    MessageGroupId=record.get("attributes", {}).get(
                        "MessageGroupId", "parked"
                    ),
                    MessageDeduplicationId=record["messageId"],
                    # The 00:05 UTC redrive moves only messages carrying this mark back to the queue.
                    MessageAttributes={
                        "parked": {
                            "DataType": "String",
                            "StringValue": "daily-cost-stop",
                        }
                    },
                )
                print(
                    json.dumps(
                        {
                            "parked": record["messageId"],
                            "spent_usd": round(spent, 4),
                            "cap_usd": cap,
                        }
                    )
                )
                continue
            sid = session_id(message, record)
            resp = _client("bedrock-agentcore").invoke_agent_runtime(
                agentRuntimeArn=os.environ["RUNTIME_ARN"],
                qualifier="DEFAULT",
                runtimeSessionId=sid,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(message).encode(),
            )
            print(
                json.dumps(
                    {
                        "session": sid,
                        "response": resp["response"].read(500).decode(errors="replace"),
                    }
                )
            )
        except Exception as e:  # noqa: BLE001 - SQS retries the record, then the DLQ keeps it
            print(
                f"dispatch failed for {record.get('messageId')}: {type(e).__name__}: {e}"
            )
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
