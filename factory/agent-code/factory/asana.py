"""Asana as the factory's task source, without an Asana credential in AWS.

The cloud never calls Asana. Two DynamoDB tables carry everything:

  inbox   software-factory-asana-snapshot: one item per project, the whole board, written by the
          workstation sync (factory/sync/asana_sync.py) through an Asana MCP server under the
          operator's own Asana session. Agents read tasks only from here.
  outbox  software-factory-asana-ops: the changes an agent wants (create, move, comment, complete).
          The sync applies them and sets their status. With publish off an op is written as
          dry_run, which the sync never applies, so a dry run is still reviewable in the table.

Board flow (section names are the contract with the board, and with the sync):

  Needs design   -> the designer writes a spec as a comment and moves the task to Design review
  Design review  -> a human reads the spec; moving the task to Ready for agent is the approval
  Ready for agent -> the developer takes it: Agent: in progress, then In review with the PR
  In review      -> the reviewer comments its review of any PR that names the task
  Done           -> the merge handler moves the task here when that PR merges

Who may start work: the factory acts on a task only when the person who moved it into its current
section is in config.ASANA_USERS. Every write the sync makes is under the maintainer's identity, so
the factory's own moves are recognised by the MARKER prefix on its comments, not by the author.
Specs and backlog stay in this private project; only pull requests are public.
"""

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta

from factory import config

PROJECT = os.environ.get(
    "FACTORY_ASANA_PROJECT", ""
)  # from -c asana_project; never in the repo

NEEDS_DESIGN, DESIGN_REVIEW = "Needs design", "Design review"
READY, IN_PROGRESS, IN_REVIEW = "Ready for agent", "Agent: in progress", "In review"
DONE, NEEDS_SCOPING, BACKLOG = "Done", "Needs scoping", "Backlog"
# Every section the factory reads or writes. Anything else on the board (the team's own sections)
# is left alone, apart from the PM's comment-only reconcile.
SECTIONS = (
    NEEDS_SCOPING,
    NEEDS_DESIGN,
    DESIGN_REVIEW,
    READY,
    BACKLOG,
    IN_PROGRESS,
    IN_REVIEW,
    DONE,
)
# Every factory comment starts with this, which is how a human's comment is told apart from ours.
MARKER = "[factory]"
SPEC_HEADER = f"{MARKER} Spec"
DIGEST_TASK = "Factory digest"
MAX_SNAPSHOT_AGE = timedelta(
    hours=6
)  # the sync runs every 15 min; 6 h means it has stopped

# How a pull request names its task: the factory's own branch, a teammate's asana/<gid> branch,
# or an "Asana-Task: <gid>" line in the body. A pasted task URL works too.
_GID = r"(\d{6,20})"
_BRANCH_REF = re.compile(rf"^(?:factory/task|asana)/{_GID}(?:\b|[-_/])")
_BODY_REFS = (
    re.compile(rf"(?im)^\s*Asana-Task:\s*{_GID}\s*$"),
    re.compile(rf"app\.asana\.com/\S*?/task/{_GID}"),
)


class StaleSnapshot(Exception):
    pass


def enabled() -> bool:
    return config.TRACKER == "asana"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _table(session, env: str):
    name = os.environ.get(env)
    return session.resource("dynamodb").Table(name) if (session and name) else None


def load_snapshot(session, project: str = "") -> dict:
    """The board as the sync last saw it. Raises StaleSnapshot when missing or too old to act on."""
    project = project or PROJECT
    if not project:
        raise StaleSnapshot("FACTORY_ASANA_PROJECT is not set")
    table = _table(session, "FACTORY_ASANA_SNAPSHOT_TABLE")
    item = table.get_item(Key={"project": project}).get("Item") if table else None
    if not item:
        raise StaleSnapshot("no Asana snapshot yet: is the workstation sync running?")
    at = item.get("snapshot_at", "")
    if datetime.fromisoformat(at) < datetime.now(UTC) - MAX_SNAPSHOT_AGE:
        raise StaleSnapshot(
            f"Asana snapshot from {at} is older than 6 h: is the sync running?"
        )
    tasks = item.get("tasks") or []
    item["tasks"] = json.loads(tasks) if isinstance(tasks, str) else list(tasks)
    return item


def allowed(task: dict) -> bool:
    """Did an allowlisted person put this task where it is? Fail closed on an unknown mover."""
    return config.asana_user_allowed(task.get("moved_by") or "")


# The project's Type field, as conventional-commit types for the developer's PR title.
COMMIT_TYPE = {
    "Feature": "feat",
    "Bug": "fix",
    "Docs": "docs",
    "Debt": "refactor",
    "Security": "fix",
    "Test": "test",
}


def priority_rank(task: dict) -> int:
    """P0 first, then P1, P2; a task with no priority comes last."""
    p = str(task.get("priority") or "")
    return int(p[1]) if len(p) > 1 and p[0] == "P" and p[1].isdigit() else 9


def work_order(task: dict) -> tuple:
    """Highest priority first, then the task a human moved earliest."""
    return (priority_rank(task), task.get("moved_at") or "")


def commit_title(title: str, task_type: str) -> str:
    """The PR title with the Type's conventional-commit prefix, replacing any the model chose."""
    kind = COMMIT_TYPE.get(task_type or "")
    if not kind:
        return title
    rest = re.sub(r"^[a-z]+(\([^)]*\))?!?:\s*", "", title.strip())
    return f"{kind}: {rest}"[:72]


def in_section(tasks: list[dict], section: str) -> list[dict]:
    return [t for t in tasks if t.get("section") == section and not t.get("completed")]


def _human_comments(task: dict) -> list[dict]:
    """Comments by an allowlisted person, oldest first. Any project member can comment, and a
    comment reaches a model prompt, so a non-allowlisted one is dropped here, not later."""
    return sorted(
        (
            c
            for c in task.get("comments") or []
            if not str(c.get("text", "")).startswith(MARKER)
            and config.asana_user_allowed(c.get("by") or "")
        ),
        key=lambda c: c.get("at", ""),
    )


def human_activity(task: dict) -> str:
    """When an allowlisted human last acted on the task: the move into its section, or a comment."""
    return max(
        [task.get("moved_at") or ""] + [c.get("at", "") for c in _human_comments(task)]
    )


def latest_spec(task: dict) -> tuple[str, list[str]]:
    """The designer's newest spec on the task, and the allowlisted comments that came after it."""
    specs = sorted(
        (
            c
            for c in task.get("comments") or []
            if str(c.get("text", "")).startswith(SPEC_HEADER)
        ),
        key=lambda c: c.get("at", ""),
    )
    if not specs:
        return "", [c["text"] for c in _human_comments(task)]
    at = specs[-1].get("at", "")
    return specs[-1]["text"], [
        c["text"] for c in _human_comments(task) if c.get("at", "") > at
    ]


def task_ref(pr: dict) -> str:
    """The Asana task gid a pull request names, or ''."""
    if m := _BRANCH_REF.match(str((pr.get("head") or {}).get("ref", ""))):
        return m.group(1)
    body = str(pr.get("body") or "")
    for pattern in _BODY_REFS:
        if m := pattern.search(body):
            return m.group(1)
    return ""


class Board:
    """Outbox writes for one agent run. Same shape as issues.Tracker, so the agents do not care which."""

    def __init__(
        self, session, agent: str, run_id: str, publish: bool, project: str = ""
    ):
        self.agent, self.run_id, self.publish = agent, run_id, publish
        self.project = project or PROJECT
        self.ops: list[dict] = []
        self.table = _table(session, "FACTORY_ASANA_OPS_TABLE")

    def _op(self, op: str, args: dict, key: list) -> str:
        digest = hashlib.sha256(
            json.dumps([op, *key], sort_keys=True).encode()
        ).hexdigest()
        item = {
            "op_id": f"factory:{self.agent}:{digest[:16]}",
            "op": op,
            "project": self.project,
            "args": args,
            # dry_run ops are recorded for review and never applied by the sync
            "status": "pending" if self.publish else "dry_run",
            "created_at": now(),
            "run_id": self.run_id,
            "agent": self.agent,
        }
        self.ops.append({**item, "applied": self.publish})
        if self.table:
            try:
                self.table.put_item(
                    Item=item, ConditionExpression="attribute_not_exists(op_id)"
                )
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                self.ops[-1]["duplicate"] = True  # same idempotency key already queued
        return item["op_id"]

    # --- the issues.Tracker interface, in board terms -----------------------
    def comment(self, gid, text: str) -> None:
        text = text if text.startswith(MARKER) else f"{MARKER} {text}"
        self._op(
            "comment",
            {"task_gid": str(gid), "text": text},
            [str(gid), text, self.run_id],
        )

    def move(self, gid, section: str, comment: str = "") -> None:
        if section not in SECTIONS:
            raise ValueError(f"not a factory section: {section!r}")
        gid = str(gid)
        self._op(
            "move_task",
            {"task_gid": gid, "section": section},
            [gid, section, self.run_id],
        )
        if section == DONE:
            self._op("complete_task", {"task_gid": gid}, [gid, "done", self.run_id])
        if comment:
            self.comment(gid, comment)

    def create(self, section: str, name: str, notes: str) -> str:
        """A new task. Keyed on its title, not the run: proposing the same task twice creates one."""
        if section not in SECTIONS:
            raise ValueError(f"not a factory section: {section!r}")
        key = [self.project, " ".join(name.lower().split())]
        return self._op(
            "create_task",
            {"section": section, "name": name[:250], "notes": f"{notes}\n\n{MARKER}"},
            key,
        )


def apply_plan(board: Board, reconcile: list[dict], new: list[dict]) -> None:
    """plan_board's output on the board. Nothing the PM proposes skips a human:

    new tasks land in Design review (or Backlog for the large ones), never in Ready for agent;
    "done" and "needs scoping" verdicts are comments the human acts on, not moves.
    """
    for r in reconcile:
        prefix = {
            DONE: "looks delivered; move it to Done if you agree. ",
            NEEDS_SCOPING: "may need rescoping. ",
        }.get(r["section"], "")
        board.comment(r["task"], f"PM: {prefix}{r['comment']}")
    for t in new:
        section = BACKLOG if t.get("section") == BACKLOG else DESIGN_REVIEW
        t["section"] = section
        t["op_id"] = board.create(section, t["title"], t["body"])


def snapshot_tasks_for_pm(snapshot: dict) -> list[dict]:
    """The PM's view of the board: open tasks an allowlisted person created, plus the factory's own."""
    from factory.pm import parse_notes  # pm imports this module

    out = []
    for t in snapshot["tasks"]:
        if t.get("completed") or t.get("name") == DIGEST_TASK:
            continue
        ours = MARKER in (t.get("notes") or "")
        if not (ours or config.asana_user_allowed(t.get("created_by") or "")):
            continue
        out.append(
            {
                "gid": t["gid"],
                "title": t["name"],
                "url": "",  # the permalink is internal; the model does not need it
                "labels": [t.get("section") or ""],
                "completed": False,
                "factory_pm": ours,
                "notes": (t.get("notes") or "")[:2000],
                **parse_notes(t.get("notes") or ""),
            }
        )
    return out


def developer_task(t: dict) -> dict:
    """What the developer is given: the task, the approved spec, and what humans said after it."""
    from factory.pm import parse_notes

    spec, after = latest_spec(t)
    notes = t.get("notes") or ""
    text = "\n\n".join(
        x
        for x in (
            notes,
            spec.removeprefix(SPEC_HEADER).strip(),
            "Reviewer notes on the spec:\n" + "\n".join(f"- {a}" for a in after)
            if after
            else "",
        )
        if x
    )
    fields = parse_notes(spec or notes)
    return {
        "gid": str(t["gid"]),
        "title": t["name"],
        "type": t.get("type") or "",
        "priority": t.get("priority") or "",
        "notes": text,
        "url": "",
        **fields,
    }
