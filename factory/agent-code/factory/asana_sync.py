"""Cloud Asana sync (Lambda `software-factory-asana-sync-cloud`, every 30 min): apply pending outbox ops
(asana.py) and rebuild the snapshot, for project asana.PROJECT only. The Mac sync stays as a fallback; both
claim an op (pending -> applying) before applying it, so neither applies one twice.
The Asana side is one small client interface, so either backend the user picks plugs in unchanged:
  - RestClient: Asana REST API with a PAT read from Secrets Manager (the backend in use).
  - an MCP client for Asana's MCP server behind an AgentCore Gateway target (OAuth outbound through
    AgentCore Identity): implement the same six methods by calling the Gateway's tools.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Protocol

from factory import asana


class AsanaClient(Protocol):
    def sections(self, project: str) -> dict[str, str]: ...  # name -> gid
    def tasks(
        self, project: str
    ) -> list[dict]: ...  # snapshot task shape (asana.Board.tasks)
    def create_section(self, project: str, name: str) -> str: ...
    def create_task(
        self, project: str, section_gid: str, name: str, notes: str
    ) -> str: ...
    def move_task(self, task_gid: str, section_gid: str) -> None: ...
    def comment(self, task_gid: str, text: str) -> None: ...
    def update_task(self, task_gid: str, fields: dict) -> None: ...


def apply_op(op: dict, client: AsanaClient, sections: dict[str, str]) -> str:
    """Apply one outbox op; returns the Asana gid it produced or touched. Sections are resolved by name and
    created when missing, so ensure_section is idempotent."""
    a, project = op["args"], op["project"]

    def section(name: str) -> str:
        if name not in sections:
            sections[name] = client.create_section(project, name)
        return sections[name]

    kind = op["op"]
    if kind == "ensure_section":
        return section(a["section"])
    if kind == "create_task":
        return client.create_task(project, section(a["section"]), a["name"], a["notes"])
    if kind == "move_task":
        client.move_task(a["task_gid"], section(a["section"]))
        return a["task_gid"]
    if kind == "comment":
        client.comment(a["task_gid"], a["text"])
        return a["task_gid"]
    if kind == "update_task":
        allowed = {
            k: v for k, v in a["fields"].items() if k in ("completed",)
        }  # nothing else is ever sent
        client.update_task(a["task_gid"], allowed)
        return a["task_gid"]
    raise ValueError(f"unknown op {kind!r}")


def apply_pending(
    ops: list[dict], client: AsanaClient, project: str = asana.PROJECT, claim=None
) -> list[dict]:
    """Apply pending ops for the real project in creation order; one failure does not stop the rest.
    `claim(op) -> bool` reserves an op first (False: another sync took it). An op naming a task that is not
    in the project is refused. Returns the status updates to write back (op_id, status, asana_gid | error)."""
    sections = client.sections(project)
    in_project = {t["gid"] for t in client.tasks(project)}
    out = []
    for op in sorted(ops, key=lambda o: o["created_at"]):
        if op.get("status") != "pending" or op.get("project") != project:
            continue  # dry_run, superseded, test-snapshot ops are never applied
        if claim and not claim(op):
            continue
        gid = (op.get("args") or {}).get("task_gid")
        if gid is not None and gid not in in_project:
            out.append(
                {
                    "op_id": op["op_id"],
                    "status": "failed",
                    "error": f"task {gid} is not in project {project}",
                }
            )
            continue
        try:
            out.append(
                {
                    "op_id": op["op_id"],
                    "status": "applied",
                    "asana_gid": apply_op(op, client, sections),
                }
            )
        except (RuntimeError, KeyError, ValueError) as e:
            out.append(
                {"op_id": op["op_id"], "status": "failed", "error": str(e)[:300]}
            )
    return out


def snapshot(client: AsanaClient, project: str = asana.PROJECT) -> dict:
    return {
        "project": project,
        "snapshot_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sections": list(client.sections(project)),
        "tasks": client.tasks(project),
    }


class RestClient:
    """Asana REST API with a personal access token (bearer). The token never leaves this object."""

    API = "https://app.asana.com/api/1.0"

    def __init__(self, token: str):
        self._token = token

    def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = self.API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps({"data": body}).encode() if body is not None else None,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(
                req, timeout=30
            ) as r:  # fixed https://app.asana.com base
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Asana {method} {path} -> {e.code}: {e.read(300).decode(errors='replace')}"
            ) from None

    def _list(self, path: str, params: dict) -> list[dict]:
        out, params = [], {**params, "limit": 100}
        while True:
            page = self._call("GET", path, params=params)
            out += page.get("data", [])
            offset = (page.get("next_page") or {}).get("offset")
            if not offset:
                return out
            params["offset"] = offset

    def sections(self, project):
        return {
            s["name"]: s["gid"] for s in self._list(f"/projects/{project}/sections", {})
        }

    def tasks(self, project):
        fields = (
            "name,notes,completed,modified_at,memberships.section.name,permalink_url"
        )
        return [
            {
                "gid": t["gid"],
                "name": t["name"],
                "section": ((t.get("memberships") or [{}])[0].get("section") or {}).get(
                    "name"
                ),
                "notes": t.get("notes", ""),
                "completed": t.get("completed", False),
                "modified_at": t.get("modified_at"),
                "permalink_url": t.get("permalink_url"),
            }
            for t in self._list(f"/projects/{project}/tasks", {"opt_fields": fields})
        ]

    def create_section(self, project, name):
        return self._call("POST", f"/projects/{project}/sections", {"name": name})[
            "data"
        ]["gid"]

    def create_task(self, project, section_gid, name, notes):
        body = {
            "name": name,
            "notes": notes,
            "projects": [project],
            "memberships": [{"project": project, "section": section_gid}],
        }
        return self._call("POST", "/tasks", body)["data"]["gid"]

    def move_task(self, task_gid, section_gid):
        self._call("POST", f"/sections/{section_gid}/addTask", {"task": task_gid})

    def comment(self, task_gid, text):
        self._call("POST", f"/tasks/{task_gid}/stories", {"text": text})

    def update_task(self, task_gid, fields):
        self._call("PUT", f"/tasks/{task_gid}", fields)


# --- Lambda entry point ------------------------------------------------------------------------------
def _token(sm, name: str) -> str:
    """The PAT from Secrets Manager: a raw string or {"token": ...}. '' when missing or empty. Never logged."""
    try:
        raw = sm.get_secret_value(SecretId=name)["SecretString"].strip()
    except sm.exceptions.ResourceNotFoundException:
        return ""
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return str(val.get("token") or "") if isinstance(val, dict) else ""


def handler(event, context):
    import os
    import time
    import uuid

    import boto3
    from boto3.dynamodb.conditions import Key

    ddb = boto3.resource("dynamodb")
    ops_t = ddb.Table(os.environ["FACTORY_ASANA_OPS_TABLE"])
    snap_t = ddb.Table(os.environ["FACTORY_ASANA_SNAPSHOT_TABLE"])
    runs_t = ddb.Table(os.environ["FACTORY_RUNS_TABLE"])
    started = time.time()

    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    row = {
        "run_id": uuid.uuid4().hex[:12],
        "agent": "asana_sync",
        "started_at": now(),
        "est_cost_usd": 0,
    }

    def finish(**fields):
        row.update(fields, duration_s=str(round(time.time() - started, 1)))
        runs_t.put_item(
            Item={
                **row,
                "pk": "agent#asana_sync",
                "sk": f"{row['started_at']}#{row['run_id']}",
            }
        )
        print(json.dumps({"factory_run": row}, default=str))
        return row

    token = _token(boto3.client("secretsmanager"), os.environ["ASANA_TOKEN_SECRET"])
    if not token:
        return finish(
            outcome="no_credentials", reason="secret missing or empty; nothing done"
        )
    client = RestClient(token)
    pending, kwargs = (
        [],
        {
            "IndexName": "by_status",
            "KeyConditionExpression": Key("status").eq("pending"),
        },
    )
    while True:
        page = ops_t.query(**kwargs)
        pending += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def claim(op) -> bool:
        try:
            ops_t.update_item(
                Key={"op_id": op["op_id"]},
                UpdateExpression="SET #s = :applying",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":applying": "applying",
                    ":pending": "pending",
                },
            )
            return True
        except ops_t.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    try:
        results = apply_pending(pending, client, asana.PROJECT, claim)
    except (
        RuntimeError
    ) as e:  # Asana refused the token or is down before any op was claimed
        return finish(outcome="failed", reason=str(e)[:300])
    for r in results:
        ops_t.update_item(
            Key={"op_id": r["op_id"]},
            UpdateExpression="SET #s = :s, applied_at = :at, asana_gid = :g, #err = :e",
            ExpressionAttributeNames={"#s": "status", "#err": "error"},
            ExpressionAttributeValues={
                ":s": r["status"],
                ":at": now(),
                ":g": r.get("asana_gid", ""),
                ":e": r.get("error", ""),
            },
        )
    snap_t.put_item(Item=snapshot(client, asana.PROJECT))
    failed = [r for r in results if r["status"] == "failed"]
    return finish(
        outcome="synced" if not failed else "synced_with_failures",
        reason=f"{len(results) - len(failed)} applied, {len(failed)} failed, snapshot refreshed",
        failed_ops=json.dumps(failed)[:20000],
    )
