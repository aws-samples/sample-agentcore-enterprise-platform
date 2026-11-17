"""Cloud Asana sync, backend-agnostic and NOT wired: no role, schedule, secret or Gateway exists.

Apply pending outbox ops (asana.py) and rebuild the snapshot for project asana.PROJECT only. The Asana side is
the AsanaClient interface below, so the backend the user picks plugs in unchanged. Personal access tokens are
blocked in the Asana org, so the candidate is Asana's MCP server (mcp.asana.com/v2/mcp) with a user OAuth
token from AgentCore Identity; MCP tokens are valid only for the MCP server, not the REST API. Both this sync
and the Mac sync must claim an op (pending -> applying) before applying it, so neither applies one twice.
"""

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
