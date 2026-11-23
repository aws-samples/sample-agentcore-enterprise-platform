"""Workstation Asana sync: the factory's only path to the board.

Every run (a systemd timer, every 15 minutes):
  1. reads the pending ops the agents queued in software-factory-asana-ops,
  2. applies them to the board through the Enterprise Asana MCP (`enterprise-asana-mcp`, stdio),
     under the maintainer's own Midway session,
  3. writes the board, as one item, to software-factory-asana-snapshot for the agents to read.

No Asana credential exists in AWS; the factory sees the board only through step 3 and changes it
only through step 1. While Midway has expired the MCP refuses, the run exits non-zero, and after
6 h the agents skip with stale_snapshot instead of acting on an old board. `mwinit` resumes it.

This side enforces the board rules again, whatever the outbox says, because the outbox is written
by agents that read untrusted text:
  * no op moves or creates a task into Ready for agent or Needs design. Those two sections are
    the human gates: only a person puts a task there, so the factory can never approve itself;
  * every op names a task already in this project, and a section the factory owns;
  * every comment starts with the factory marker, so a person can always tell ours from theirs.

    python factory/sync/asana_sync.py --project <gid> [--dry-run] [--profile P] [--region R]
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-code"))

from factory import asana

GATES = {asana.READY, asana.NEEDS_DESIGN}  # only a human moves a task into these
# Tasks in these sections carry their stories in the snapshot: who moved them there, and the
# comments. Elsewhere only the task fields are kept, which keeps a poll to a few dozen calls.
WITH_STORIES = {
    asana.NEEDS_DESIGN,
    asana.DESIGN_REVIEW,
    asana.READY,
    asana.IN_PROGRESS,
    asana.IN_REVIEW,
}
OPS = ("create_task", "move_task", "comment", "complete_task")
_MOVED = re.compile(r' to "(?P<to>[^"]+)"')


def iso(t: str | None) -> str:
    """Asana's 2026-11-17T12:40:54.565Z as the runs table writes times: seconds, +00:00."""
    if not t:
        return ""
    return datetime.fromisoformat(t).astimezone(UTC).isoformat(timespec="seconds")


# --- the MCP client ------------------------------------------------------------------------


class McpError(RuntimeError):
    pass


class Mcp:
    """A minimal MCP stdio client: initialize once, then tools/call. No SDK needed."""

    def __init__(self, command: list[str]):
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._id = 0
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "software-factory-asana-sync", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise McpError(
                    f"MCP server exited: {self.proc.stderr.read()[-600:]} (run mwinit?)"
                )
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise McpError(f"{method}: {msg['error']}")
                return msg["result"]

    def call(self, tool: str, **args) -> dict:
        res = self._rpc(
            "tools/call",
            {
                "name": f"asana___{tool}",
                "arguments": {k: str(v) for k, v in args.items()},
            },
        )
        text = "".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise McpError(f"{tool}: {text[:400]}")
        data = json.loads(text or "{}")
        # Writes answer {"APIOutput": {"Response": {...}}}, reads answer the Asana body itself.
        data = (data.get("APIOutput") or {}).get("Response", data)
        if "errors" in data:
            raise McpError(f"{tool}: {data['errors']}")
        return data

    def pages(self, tool: str, **args) -> list[dict]:
        out, offset = [], None
        while True:
            page = self.call(
                tool, **args, limit=100, **({"offset": offset} if offset else {})
            )
            out += page.get("data", [])
            offset = (page.get("next_page") or {}).get("offset")
            if not offset:
                return out

    def close(self) -> None:
        self.proc.terminate()


class Board:
    """The board operations the sync needs, over the MCP."""

    FIELDS = "name,notes,completed,modified_at,created_at,created_by,memberships.section.name,memberships.project.gid"

    def __init__(self, mcp: Mcp, project: str):
        self.mcp, self.project = mcp, project

    def sections(self) -> dict[str, str]:
        return {
            s["name"]: s["gid"]
            for s in self.mcp.pages("GetProjectSections", project_gid=self.project)
        }

    def tasks(self) -> list[dict]:
        out = []
        for t in self.mcp.pages(
            "GetTasksFromProject", project_gid=self.project, opt_fields=self.FIELDS
        ):
            section = next(
                (
                    (m.get("section") or {}).get("name")
                    for m in t.get("memberships") or []
                    if (m.get("project") or {}).get("gid", self.project) == self.project
                ),
                None,
            )
            out.append(
                {
                    "gid": t["gid"],
                    "name": t.get("name", ""),
                    "notes": (t.get("notes") or "")[:8000],
                    "section": section,
                    "completed": bool(t.get("completed")),
                    "modified_at": iso(t.get("modified_at")),
                    "created_by": (t.get("created_by") or {}).get("gid", ""),
                    "moved_by": (t.get("created_by") or {}).get("gid", ""),
                    "moved_at": iso(t.get("created_at")),
                }
            )
        return out

    def stories(self, gid: str) -> list[dict]:
        return self.mcp.pages("GetStoriesForTask", task_gid=gid)

    def create_task(self, section_gid: str, name: str, notes: str) -> str:
        gid = self.mcp.call("CreateTask", project=self.project, name=name, notes=notes)[
            "data"
        ]["gid"]
        self.move_task(gid, section_gid)
        return gid

    def move_task(self, gid: str, section_gid: str) -> None:
        self.mcp.call("AddTaskToSection", section_gid=section_gid, task=gid)

    def comment(self, gid: str, text: str) -> None:
        self.mcp.call("CreateTaskStory", task_gid=gid, text=text)

    def complete_task(self, gid: str) -> None:
        self.mcp.call("UpdateTask", task_gid=gid, completed="true")


# --- the rules: pure functions over a Board, so tests need no MCP ---------------------------


def with_stories(task: dict, stories: list[dict]) -> dict:
    """moved_by/moved_at: the latest move into the task's current section (else its creator);
    comments: who wrote what, when. The developer and designer gate on moved_by."""
    for s in stories:
        if s.get("resource_subtype") == "section_changed":
            m = _MOVED.search(s.get("text") or "")
            if m and m.group("to") == task["section"]:
                task["moved_by"] = (s.get("created_by") or {}).get("gid", "")
                task["moved_at"] = iso(s.get("created_at"))
    task["comments"] = [
        {
            "by": (s.get("created_by") or {}).get("gid", ""),
            "at": iso(s.get("created_at")),
            "text": (s.get("text") or "")[:4000],
        }
        for s in stories
        if s.get("resource_subtype") == "comment_added"
    ][-30:]
    return task


def snapshot(board: Board) -> dict:
    tasks = board.tasks()
    for t in tasks:
        if t["section"] in WITH_STORIES and not t["completed"]:
            with_stories(t, board.stories(t["gid"]))
    return {
        "project": board.project,
        "snapshot_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sections": list(board.sections()),
        "tasks": json.dumps(
            tasks
        ),  # one string: DynamoDB maps would need every field typed
        "sync_host": socket.gethostname(),
    }


def refuse(op: dict, project: str, in_project: set[str]) -> str:
    """Why this op must not reach the board, or ''."""
    a = op.get("args") or {}
    if op.get("project") != project:
        return "op is for another project"
    if op.get("op") not in OPS:
        return f"unknown op {op.get('op')!r}"
    section = a.get("section")
    if op["op"] in ("create_task", "move_task"):
        if section in GATES:
            return f"only a person moves a task into {section!r}"
        if section not in asana.SECTIONS:
            return f"{section!r} is not a factory section"
    if op["op"] != "create_task" and str(a.get("task_gid")) not in in_project:
        return f"task {a.get('task_gid')} is not in project {project}"
    if op["op"] == "comment" and not str(a.get("text", "")).startswith(asana.MARKER):
        return "comment does not carry the factory marker"
    return ""


def apply_op(op: dict, board: Board, sections: dict[str, str]) -> str:
    a = op["args"]
    if op["op"] == "create_task":
        notes = (
            a["notes"]
            if asana.MARKER in a["notes"]
            else f"{a['notes']}\n\n{asana.MARKER}"
        )
        return board.create_task(sections[a["section"]], a["name"][:250], notes[:60000])
    gid = str(a["task_gid"])
    if op["op"] == "move_task":
        board.move_task(gid, sections[a["section"]])
    elif op["op"] == "comment":
        board.comment(gid, a["text"][:60000])
    elif op["op"] == "complete_task":
        board.complete_task(gid)
    return gid


def apply_pending(ops: list[dict], board: Board, claim=lambda op: True) -> list[dict]:
    """Apply pending ops oldest first. One failure does not stop the rest. Returns status updates."""
    sections = board.sections()
    in_project = {t["gid"] for t in board.tasks()}
    out = []
    for op in sorted(ops, key=lambda o: o.get("created_at", "")):
        if op.get("status") != "pending" or not claim(op):
            continue
        if why := refuse(op, board.project, in_project):
            out.append({"op_id": op["op_id"], "status": "refused", "error": why})
            continue
        if op["args"].get("section") and op["args"]["section"] not in sections:
            out.append(
                {
                    "op_id": op["op_id"],
                    "status": "failed",
                    "error": f"section {op['args']['section']!r} is not on the board",
                }
            )
            continue
        try:
            gid = apply_op(op, board, sections)
            out.append({"op_id": op["op_id"], "status": "applied", "asana_gid": gid})
            if op["op"] == "create_task":
                in_project.add(gid)
        except (McpError, KeyError, ValueError) as e:
            out.append(
                {"op_id": op["op_id"], "status": "failed", "error": str(e)[:300]}
            )
    return out


# --- DynamoDB ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import boto3
    from boto3.dynamodb.conditions import Key

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--project",
        default=os.environ.get("FACTORY_ASANA_PROJECT"),
        required=not os.environ.get("FACTORY_ASANA_PROJECT"),
    )
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--ops-table", default="software-factory-asana-ops")
    p.add_argument("--snapshot-table", default="software-factory-asana-snapshot")
    p.add_argument("--mcp", default="enterprise-asana-mcp")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="read the board and the outbox; write nothing anywhere",
    )
    args = p.parse_args(argv)

    mcp = Mcp([args.mcp])
    try:
        board = Board(mcp, args.project)
        if args.dry_run and not args.profile:
            snap = snapshot(board)
            tasks = json.loads(snap["tasks"])
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "sections": snap["sections"],
                        "tasks": len(tasks),
                        "by_section": {
                            s: sum(t["section"] == s for t in tasks)
                            for s in snap["sections"]
                        },
                    },
                    indent=1,
                )
            )
            return 0
        ddb = boto3.Session(
            profile_name=args.profile, region_name=args.region
        ).resource("dynamodb")
        ops_t, snap_t = ddb.Table(args.ops_table), ddb.Table(args.snapshot_table)
        pending, kw = (
            [],
            {
                "IndexName": "by_status",
                "KeyConditionExpression": Key("status").eq("pending"),
            },
        )
        while True:
            page = ops_t.query(**kw)
            pending += [o for o in page["Items"] if o.get("project") == args.project]
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        if args.dry_run:
            snap = snapshot(board)
            in_project = {t["gid"] for t in json.loads(snap["tasks"])}
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "pending": [
                            {
                                "op_id": o["op_id"],
                                "op": o["op"],
                                "args": o["args"],
                                "refused": refuse(o, args.project, in_project),
                            }
                            for o in pending
                        ],
                    },
                    indent=1,
                    default=str,
                )
            )
            return 0

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
                return False  # another sync took it

        results = apply_pending(pending, board, claim)
        at = datetime.now(UTC).isoformat(timespec="seconds")
        for r in results:
            ops_t.update_item(
                Key={"op_id": r["op_id"]},
                UpdateExpression="SET #s = :s, applied_at = :at, asana_gid = :g, #e = :e",
                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                ExpressionAttributeValues={
                    ":s": r["status"],
                    ":at": at,
                    ":g": r.get("asana_gid", ""),
                    ":e": r.get("error", ""),
                },
            )
        snap_t.put_item(Item=snapshot(board))
        bad = [r for r in results if r["status"] != "applied"]
        print(
            json.dumps(
                {
                    "applied": len(results) - len(bad),
                    "not_applied": bad,
                    "snapshot_at": at,
                },
                default=str,
            )
        )
        return 0
    except McpError as e:
        print(f"asana sync: {e}", file=sys.stderr)
        return 2
    finally:
        mcp.close()


if __name__ == "__main__":
    sys.exit(main())
