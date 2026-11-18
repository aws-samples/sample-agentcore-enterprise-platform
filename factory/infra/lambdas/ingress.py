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
import re
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
    """Expected GitHub App installation id from SSM; '' while the parameter is 'none' (App not set up yet).

    Kept out of the App secret so the ingress never holds the App's private key.
    """
    if _cache["installation"] is not None and time.time() - _cache["inst_at"] < 300:
        return _cache["installation"]
    raw = _client("ssm").get_parameter(Name=os.environ["INSTALLATION_PARAM"])[
        "Parameter"
    ]["Value"]
    value = "" if raw.strip() == "none" else raw.strip()
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


REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

# Commands a comment (or the factory:ready label) can start. Ingress only parses; the runtime checks
# that the sender has write access, because this function holds no GitHub credential.
ISSUE_COMMANDS = {"/pm", "/dev", "/approve", "/reject"}
PROPOSAL_IDS = re.compile(r"^(all|P-[0-9a-f]{6})( P-[0-9a-f]{6})*$")
MAX_PM_TEXT = 4000
MAX_COMMANDS_PER_HOUR = 10  # per issue, per UTC hour
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}  # GitHub's author_association on a comment


def _command(payload: dict, event_name: str) -> dict | None:
    if event_name == "issues":
        if (payload.get("label") or {}).get("name") != "factory:ready":
            return None
        return {"command": "ready", "args": ""}
    text = ((payload.get("comment") or {}).get("body") or "").strip()
    parts = text.split(None, 1)
    if not parts or parts[0].lower() not in ISSUE_COMMANDS:
        return None
    name, args = parts[0].lower()[1:], (parts[1].strip() if len(parts) > 1 else "")
    if name == "pm" and not 0 < len(args) <= MAX_PM_TEXT:
        return None
    if name in ("approve", "reject") and (
        not PROPOSAL_IDS.match(args) or (name == "reject" and args == "all")
    ):
        return None
    return {"command": name, "args": args}


def _rate_limited(issue: int) -> bool:
    """Counts commands per issue per hour in the dedupe table (rl# keys, expire after 2 hours)."""
    now = int(time.time())
    resp = _client("dynamodb").Table(os.environ["DEDUPE_TABLE"]).update_item(
        Key={"delivery_id": f"rl#{issue}#{now // 3600}"},
        UpdateExpression="ADD hits :one SET expires_at = if_not_exists(expires_at, :exp)",
        ExpressionAttributeValues={":one": 1, ":exp": now + 7200},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["hits"]) > MAX_COMMANDS_PER_HOUR


def _issue(payload: dict, event_name: str, delivery: str) -> dict:
    issue, sender = payload.get("issue") or {}, payload.get("sender") or {}
    login = str(sender.get("login", ""))
    if "pull_request" in issue:
        return _resp(202, "ignored: pull request")
    if sender.get("type") == "Bot" or login.endswith("[bot]"):
        return _resp(202, "ignored: bot sender (no loops)")
    expected = "created" if event_name == "issue_comment" else "labeled"
    if payload.get("action") != expected:
        return _resp(202, "ignored: action")
    cmd = _command(payload, event_name)
    if cmd is None:
        return _resp(202, "ignored: not a factory command")
    # Cheap filter before any write or count: comments from outsiders never reach the rate limit.
    # Label events carry no author_association; the runtime's write check covers them.
    if event_name == "issue_comment" and (payload.get("comment") or {}).get(
        "author_association"
    ) not in TRUSTED_ASSOCIATIONS:
        return _resp(202, "ignored: author is not an owner, member or collaborator")
    if not delivery or not _first_delivery(delivery):
        return _resp(202, "duplicate")
    number = int(issue["number"])
    if _rate_limited(number):
        return _resp(202, "rate limited: 10 commands per issue per hour")
    message = {
        "role": "issue_command",
        "delivery_id": delivery,
        "issue": {"number": number},
        "actor": {"login": login},
        **cmd,
    }
    try:
        _enqueue(message, group=f"issue-{number}", dedupe_id=delivery)
    except Exception:
        _forget(delivery)
        raise
    return _resp(202, "queued")


def _pull_request(payload: dict, delivery: str) -> dict:
    """Queue a review. factory/* PRs are reviewed too (agent-written code gets an agent review); Dependabot is not."""
    pr = payload.get("pull_request") or {}
    merged_factory_pr = (
        payload.get("action") == "closed"
        and pr.get("merged")
        and str((pr.get("head") or {}).get("ref", "")).startswith("factory/")
        and ((pr.get("base") or {}).get("repo") or {}).get("full_name") == REPO
    )
    if merged_factory_pr:
        if not delivery or not _first_delivery(delivery):
            return _resp(202, "duplicate")
        message = {
            "role": "merge_handler",
            "delivery_id": delivery,
            "pull_request": {"number": int(pr["number"])},
        }
        try:
            _enqueue(message, group=f"pr-{int(pr['number'])}", dedupe_id=delivery)
        except Exception:
            _forget(delivery)
            raise
        return _resp(202, "queued")
    if (
        payload.get("action") not in REVIEW_ACTIONS
        or pr.get("draft")
        or (pr.get("user") or {}).get("login") == "dependabot[bot]"
        or ((pr.get("base") or {}).get("repo") or {}).get("full_name") != REPO
    ):
        return _resp(202, "ignored: pull_request")
    if not delivery or not _first_delivery(delivery):
        return _resp(202, "duplicate")
    head_sha = str((pr.get("head") or {}).get("sha", ""))
    message = {
        "role": "pr_reviewer",
        "delivery_id": delivery,
        "pull_request": {"number": int(pr["number"]), "head_sha": head_sha},
    }
    try:
        _enqueue(message, group=f"pr-{int(pr['number'])}", dedupe_id=delivery)
    except Exception:
        _forget(delivery)
        raise
    return _resp(202, "queued")


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
    if event_name in ("issue_comment", "issues"):
        return _issue(payload, event_name, delivery)
    if event_name == "pull_request":
        return _pull_request(payload, delivery)
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
