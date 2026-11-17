"""SQS FIFO -> AgentCore Runtime. The runtime returns at once and works async (design 2.2)."""

import json
import os

import boto3

_client = None


def handler(event, context):
    global _client
    _client = _client or boto3.client("bedrock-agentcore")
    failures = []
    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            # Deterministic id: a retry reattaches to the same session instead of starting a second run.
            role = message.get("role")
            if role == "merge_handler":
                session_id = (
                    f"factory-merge-{int(message['pull_request']['number'])}".ljust(
                        33, "-"
                    )
                )
            elif role in ("product_manager", "developer"):
                # Scheduled/manual: one session per SQS message (its id is unique per delivery).
                session_id = f"factory-{role}-{record['messageId']}"[:100].ljust(
                    33, "-"
                )
            elif role == "pr_reviewer":
                pr = message["pull_request"]
                session_id = f"factory-rev-{int(pr['number'])}-{str(pr.get('head_sha', ''))[:12]}".ljust(
                    33, "-"
                )
            else:
                run = message["workflow_run"]
                session_id = f"factory-ci-{int(run['id'])}-attempt-{int(run.get('run_attempt') or 1)}".ljust(
                    33, "-"
                )
            resp = _client.invoke_agent_runtime(
                agentRuntimeArn=os.environ["RUNTIME_ARN"],
                qualifier="DEFAULT",
                runtimeSessionId=session_id,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(message).encode(),
            )
            print(
                json.dumps(
                    {
                        "session": session_id,
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
