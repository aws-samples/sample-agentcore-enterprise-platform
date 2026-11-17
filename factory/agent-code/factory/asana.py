"""Asana board access without an Asana credential in the cloud (hybrid outbox/inbox).

Outbox: agents write the Asana operations they want into the `software-factory-asana-ops` table
(status=pending); a Mac-side sync applies them with the user's own Asana session and sets status.
Inbox: the same sync writes the board as one snapshot item into `software-factory-asana-snapshot`;
agents read tasks only from it. The cloud never calls Asana.

Op item (the contract the Mac sync applies):
  op_id       idempotency key "factory:<agent>:<16 hex>"; a second put of the same key is a no-op
  op          ensure_section | create_task | move_task | comment | update_task
  project     Asana project gid
  args        op-specific map (below); sections are referenced by NAME, the sync resolves gids
  status      pending | applied | failed     created_at  ISO-8601 UTC     run_id, agent
"""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

PROJECT = os.environ.get("FACTORY_ASANA_PROJECT", "1216896369638424")
WORKSPACE = os.environ.get("FACTORY_ASANA_WORKSPACE", "8442528107068")
SECTIONS = (
    "Backlog",
    "Ready for agent",
    "Agent: in progress",
    "In review",
    "Needs scoping",
    "Done",
)
TAG = "factory:pm"
MARKER = "[factory:pm]"  # also in notes, so humans and dedupe see it without the tag
MAX_SNAPSHOT_AGE = timedelta(hours=24)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StaleSnapshot(Exception):
    pass


class Board:
    def __init__(
        self,
        session,
        agent: str,
        run_id: str,
        project: str = PROJECT,
        snapshot_key: str | None = None,
    ):
        self.agent, self.run_id, self.project = agent, run_id, project
        self.snapshot_key = snapshot_key or project
        self.dry_run = True  # the cloud never writes Asana; the sync applies the ops
        self.ops: list[dict] = []
        ddb = session.resource("dynamodb") if session else None
        self.ops_table = (
            ddb.Table(os.environ["FACTORY_ASANA_OPS_TABLE"])
            if ddb and os.environ.get("FACTORY_ASANA_OPS_TABLE")
            else None
        )
        self.snap_table = (
            ddb.Table(os.environ["FACTORY_ASANA_SNAPSHOT_TABLE"])
            if ddb and os.environ.get("FACTORY_ASANA_SNAPSHOT_TABLE")
            else None
        )
        self.snapshot_at = ""
        self.section_names: set[str] = set()

    # --- inbox ------------------------------------------------------------
    def tasks(self, require_fresh: bool = False) -> list[dict]:
        """Tasks from the snapshot. require_fresh: refuse a missing or >24h-old snapshot."""
        item = (
            self.snap_table.get_item(Key={"project": self.snapshot_key}).get("Item")
            if self.snap_table
            else None
        )
        self.snapshot_at = (item or {}).get("snapshot_at", "")
        fresh = (
            bool(item)
            and datetime.fromisoformat(self.snapshot_at)
            > datetime.now(UTC) - MAX_SNAPSHOT_AGE
        )
        if require_fresh and not fresh:
            raise StaleSnapshot(
                f"snapshot {'missing' if not item else 'from ' + self.snapshot_at} (max age 24h)"
            )
        tasks = (item or {}).get("tasks") or []
        tasks = json.loads(tasks) if isinstance(tasks, str) else list(tasks)
        self.section_names = set((item or {}).get("sections") or []) | {
            t.get("section") for t in tasks
        }
        return tasks

    # --- outbox -----------------------------------------------------------
    def _op(self, op: str, args: dict, key_parts: list[str]) -> str:
        digest = hashlib.sha256(
            json.dumps([op, *key_parts], sort_keys=True).encode()
        ).hexdigest()[:16]
        item = {
            "op_id": f"factory:{'pm' if self.agent == 'product_manager' else self.agent}:{digest}",
            "op": op,
            "project": self.project,
            "args": args,
            "status": "pending",
            "created_at": now(),
            "run_id": self.run_id,
            "agent": self.agent,
        }
        self.ops.append(item)
        if self.ops_table:
            try:
                self.ops_table.put_item(
                    Item=item, ConditionExpression="attribute_not_exists(op_id)"
                )
            except (
                self.ops_table.meta.client.exceptions.ConditionalCheckFailedException
            ):
                item["duplicate"] = (
                    True  # same idempotency key already queued or applied
                )
        return item["op_id"]

    def ensure_sections(self) -> list[str]:
        """One ensure op per section; the sync creates only the missing ones."""
        self.tasks()
        have = self.section_names
        missing = [s for s in SECTIONS if s not in have]
        for s in missing:
            self._op("ensure_section", {"section": s}, [self.project, s, self.run_id])
        return missing

    def create_task(
        self, section: str, name: str, notes: str, files: list[str] | None = None
    ) -> str:
        # Keyed on the title and files, not the run: a re-proposal of the same task never duplicates it.
        key = [self.project, " ".join(name.lower().split()), sorted(files or [])]
        return self._op(
            "create_task",
            {
                "section": section,
                "name": name,
                "notes": f"{notes}\n\n{MARKER}",
                "tags": [TAG],
            },
            [json.dumps(key)],
        )

    def move(self, task_gid: str, section: str, comment: str = "") -> None:
        self._op(
            "move_task",
            {"task_gid": task_gid, "section": section},
            [task_gid, section, self.run_id],
        )
        if section == "Done":
            self._op(
                "update_task",
                {"task_gid": task_gid, "fields": {"completed": True}},
                [task_gid, "done", self.run_id],
            )
        if comment:
            self._op(
                "comment",
                {"task_gid": task_gid, "text": comment},
                [task_gid, comment, self.run_id],
            )
