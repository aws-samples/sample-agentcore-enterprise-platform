"""Chat notifications: one message per factory run that a person should know about.

Triggered by the runs table's stream (INSERT only: every agent run writes exactly one row).
Posts to a chat webhook whose URL lives in Secrets Manager; only this function can read it.
A secret value of "none" (the placeholder) turns notifications off without a deploy.

Messages carry what the row already holds: agent, outcome, a one-line reason, the PR URL and
a link to the board task. No model output beyond the row's reason is sent.
"""

import json
import os
import time
import urllib.request

import boto3

WEBHOOK_PREFIX = "https://hooks.slack.com/"
QUIET_S = 6 * 3600  # repeated "the board is stale" notices: once per this window

# (agent, outcome) -> (emoji, headline). Outcomes not here are not announced, except failures.
EVENTS = {
    ("designer", "spec_written"): (":memo:", "Spec ready for your review"),
    ("designer", "question_asked"): (":question:", "The designer has a question"),
    ("designer", "spec_rejected"): (
        ":warning:",
        "The designer's spec failed the checks",
    ),
    ("developer", "pr_opened"): (":rocket:", "PR opened"),
    ("developer", "checks_failed"): (":x:", "The developer's change failed the checks"),
    ("developer", "needs_scoping"): (":thinking_face:", "The developer needs scoping"),
    ("pr_reviewer", "review_posted"): (":mag:", "Review posted"),
    ("ci_fixer", "fix_pr_opened"): (":wrench:", "CI fix PR opened"),
    ("merge_handler", "merged_recorded"): (":white_check_mark:", "Merged"),
    ("product_manager", "issues_updated"): (":bulb:", "The PM proposed tasks"),
    ("digest", "digest_written"): (":bar_chart:", "Daily digest posted"),
    ("digest", "digest_not_posted"): (
        ":warning:",
        "Daily digest written but not posted",
    ),
}
THROTTLED = {
    "skipped_stale_snapshot",
    "skipped_missing_permission",
    "skipped_repo_unreachable",
}

_secret = {"at": 0.0, "url": ""}


def _value(attr: dict):
    """One DynamoDB stream attribute value as plain Python (only the types run rows use)."""
    ((kind, v),) = attr.items()
    if kind == "N":
        return float(v)
    if kind == "BOOL":
        return v
    if kind == "M":
        return {k: _value(x) for k, x in v.items()}
    if kind == "L":
        return [_value(x) for x in v]
    return v


def row_of(record: dict) -> dict:
    return {k: _value(v) for k, v in record["dynamodb"]["NewImage"].items()}


def task_link(row: dict, project: str) -> str:
    trigger = str(row.get("trigger") or "")
    for prefix in ("task-", "design-"):
        gid = trigger.removeprefix(prefix)
        if trigger.startswith(prefix) and gid.isdigit() and project:
            return f"https://app.asana.com/0/{project}/{gid}"
    gid = str(row.get("task_gid") or "")
    return (
        f"https://app.asana.com/0/{project}/{gid}" if gid.isdigit() and project else ""
    )


def message(row: dict, project: str) -> tuple[str, str] | None:
    """(text, throttle key) for a row worth announcing, else None."""
    agent, outcome = str(row.get("agent") or ""), str(row.get("outcome") or "")
    reason = " ".join(str(row.get("reason") or "").split())[:300]
    if (agent, outcome) in EVENTS:
        emoji, head = EVENTS[(agent, outcome)]
        key = ""
    elif outcome == "failed":
        emoji, head, key = ":rotating_light:", f"{agent} run failed", ""
    elif outcome in THROTTLED:
        emoji, head, key = (
            ":hourglass:",
            f"{agent} skipped: {outcome}",
            f"notify#{outcome}",
        )
    else:
        return None
    parts = [f"{emoji} *{head}* ({agent})"]
    if reason and outcome not in ("pr_opened", "merged_recorded", "spec_written"):
        parts.append(reason)
    links = [u for u in (row.get("pr_url"), task_link(row, project)) if u]
    if links:
        parts.append(" · ".join(str(u) for u in links))
    cost = row.get("est_cost_usd")
    if isinstance(cost, float) and cost >= 0.01:
        parts.append(f"cost ${cost:.2f}")
    return "\n".join(parts), key


def _webhook(sm) -> str:
    if (
        time.time() - _secret["at"] > 300
    ):  # re-read every 5 minutes: rotation needs no deploy
        value = sm.get_secret_value(SecretId=os.environ["WEBHOOK_SECRET"])[
            "SecretString"
        ].strip()
        _secret.update(
            at=time.time(), url=value if value.startswith(WEBHOOK_PREFIX) else ""
        )
    return _secret["url"]


def _first_in_window(ddb, key: str) -> bool:
    table = ddb.Table(os.environ["DEDUPE_TABLE"])
    try:
        table.put_item(
            Item={"delivery_id": key, "expires_at": int(time.time()) + QUIET_S},
            ConditionExpression="attribute_not_exists(delivery_id) OR expires_at < :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def post(url: str, text: str, opener=urllib.request.urlopen) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(
        req, timeout=10
    ) as r:  # fixed https://hooks.slack.com/ prefix, checked above
        r.read()


def handler(event, context):
    project = os.environ.get("FACTORY_ASANA_PROJECT", "")
    sm, ddb = boto3.client("secretsmanager"), boto3.resource("dynamodb")
    url = _webhook(sm)
    sent = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        msg = message(row_of(record), project)
        if not msg or not url:
            continue
        text, key = msg
        if key and not _first_in_window(ddb, key):
            continue
        try:
            post(url, text)
            sent += 1
        except Exception as e:  # noqa: BLE001 - a chat outage must not block or retry the stream
            print(json.dumps({"notify_failed": type(e).__name__}))
    print(
        json.dumps(
            {
                "records": len(event.get("Records", [])),
                "sent": sent,
                "enabled": bool(url),
            }
        )
    )
    return {"sent": sent}
