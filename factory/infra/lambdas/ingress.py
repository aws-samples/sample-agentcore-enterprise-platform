"""GitHub webhook ingress: verify X-Hub-Signature-256, dedupe, filter, enqueue.

GitHub's HMAC over the raw body is the authentication. Fails closed: an unreadable
or empty secret returns 500 and enqueues nothing; a missing or wrong signature
returns 401. JSON is parsed only after the signature matches.
"""

import base64
import hashlib
import hmac
import json
import os
import time

import boto3

MAX_BODY = 1024 * 1024
REPO = os.environ.get("REPO", "aws-samples/sample-agentcore-enterprise-platform")
DEDUPE_TTL_S = 3 * 24 * 3600
_cache: dict = {"secret": b"", "at": 0.0, "installation": None, "inst_at": 0.0}
_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        _clients[name] = (
            boto3.resource("dynamodb") if name == "dynamodb" else boto3.client(name)
        )
    return _clients[name]


def _secret() -> bytes:
    if _cache["secret"] and time.time() - _cache["at"] < 300:
        return _cache["secret"]
    value = (
        _client("secretsmanager")
        .get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"]
        .encode()
    )
    if not value:
        raise ValueError("webhook secret is empty")
    _cache.update(secret=value, at=time.time())
    return value


def _installation_id() -> str:
    """installation_id from the GitHub App secret; '' while the secret is '{}' (dry run, App not created)."""
    if _cache["installation"] is not None and time.time() - _cache["inst_at"] < 300:
        return _cache["installation"]
    raw = _client("secretsmanager").get_secret_value(
        SecretId=os.environ["APP_SECRET_ARN"]
    )["SecretString"]
    value = str(json.loads(raw or "{}").get("installation_id") or "")
    _cache.update(installation=value, inst_at=time.time())
    return value


def _first_delivery(delivery_id: str) -> bool:
    table = _client("dynamodb").Table(os.environ["DEDUPE_TABLE"])
    try:
        table.put_item(
            Item={
                "delivery_id": delivery_id,
                "expires_at": int(time.time()) + DEDUPE_TTL_S,
            },
            ConditionExpression="attribute_not_exists(delivery_id)",
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _forget(delivery_id: str) -> None:
    _client("dynamodb").Table(os.environ["DEDUPE_TABLE"]).delete_item(
        Key={"delivery_id": delivery_id}
    )


def _enqueue(message: dict, group: str, dedupe_id: str) -> None:
    _client("sqs").send_message(
        QueueUrl=os.environ["QUEUE_URL"],
        MessageBody=json.dumps(message),
        MessageGroupId=group,
        MessageDeduplicationId=dedupe_id,
    )


def _resp(code: int, message: str) -> dict:
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"message": message}),
    }


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    if len(raw) > MAX_BODY:
        return _resp(413, "payload too large")
    try:
        secret = _secret()
    except Exception as e:  # noqa: BLE001 - fail closed on any secret problem
        print(f"webhook secret unavailable: {type(e).__name__}")
        return _resp(500, "unavailable")
    signature = headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not signature.startswith("sha256=") or not hmac.compare_digest(
        signature.encode(), expected.encode()
    ):
        print(
            json.dumps(
                {"event": headers.get("x-github-event", ""), "signature": "rejected"}
            )
        )
        return _resp(401, "bad signature")

    payload = json.loads(raw)
    event_name, delivery = (
        headers.get("x-github-event", ""),
        headers.get("x-github-delivery", ""),
    )
    if event_name == "ping":
        print(
            json.dumps(
                {
                    "event": "ping",
                    "delivery": delivery,
                    "hook_id": payload.get("hook_id"),
                    "signature": "ok",
                }
            )
        )
        return _resp(200, "pong")
    if payload.get("repository", {}).get("full_name") != REPO:
        return _resp(202, "ignored: repository")
    try:
        installation = _installation_id()
    except Exception as e:  # noqa: BLE001 - fail closed: never enqueue without knowing the installation
        print(f"app secret unavailable: {type(e).__name__}")
        return _resp(500, "unavailable")
    if (
        installation
        and str((payload.get("installation") or {}).get("id")) != installation
    ):
        return _resp(202, "ignored: installation")
    if event_name != "workflow_run" or payload.get("action") != "completed":
        return _resp(202, "ignored: event")
    run = payload["workflow_run"]
    if (
        run.get("conclusion") != "failure"
        or str(run.get("head_branch", "")).startswith("factory/")
        or (run.get("head_repository") or {}).get("full_name", REPO) != REPO
    ):
        return _resp(202, "ignored: not a failed run on a non-factory, non-fork branch")
    if not delivery or not _first_delivery(delivery):
        return _resp(202, "duplicate")
    keep = (
        "id",
        "name",
        "path",
        "head_branch",
        "head_sha",
        "conclusion",
        "run_attempt",
        "run_started_at",
        "html_url",
    )
    message = {
        "role": "ci_fixer",
        "delivery_id": delivery,
        "workflow_run": {
            **{k: run.get(k) for k in keep},
            "repository": {"full_name": REPO},
        },
    }
    try:
        _enqueue(message, group=f"ci-{int(run['id'])}", dedupe_id=delivery)
    except Exception:
        _forget(delivery)  # let GitHub's redelivery through
        raise
    return _resp(202, "queued")
