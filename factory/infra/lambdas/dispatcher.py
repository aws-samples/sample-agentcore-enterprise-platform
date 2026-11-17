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
            run = message["workflow_run"]
            # Deterministic id: a retry reattaches to the same session instead of starting a second run.
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
