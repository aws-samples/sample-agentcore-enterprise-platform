"""Planner: which tasks on the board need an agent right now, one job per task.

The board snapshot changes whenever the sync runs. The planner turns that snapshot into jobs:

  designer  a task an allowlisted person moved into Needs design
  developer a task an allowlisted person moved into Ready for agent
  revise    a task in In review with a maintainer comment newer than its last developer run

Each job is keyed on (role, task, the human action that caused it), so one move or one comment
starts at most one run, however many times the snapshot is rewritten. Jobs run in parallel: the
dispatcher gives each its own session. The 30-minute schedules stay as a backstop.

Pure functions: the Lambda in infra/lambdas/planner.py supplies the snapshot, the attempt times
from the runs table, and the dedupe/queue clients.
"""

from factory import asana

MAX_JOBS_PER_PASS = (
    10  # a burst of moves is drained over consecutive snapshots, not all at once
)


def _comment_since(task: dict, since: str) -> str:
    """The newest maintainer comment after `since`, or ''."""
    times = [
        c.get("at", "") for c in asana._human_comments(task) if c.get("at", "") > since
    ]
    return max(times, default="")


def jobs(tasks: list[dict], attempted: dict[str, dict[str, str]]) -> list[dict]:
    """[{role, gid, cause}] for every task needing work. attempted: role -> gid -> last run start."""
    out = []
    for t in tasks:
        if t.get("completed"):
            continue
        gid, section = str(t["gid"]), t.get("section")
        if section == asana.NEEDS_DESIGN and asana.allowed(t):
            cause = asana.human_activity(t)
            if attempted.get("designer", {}).get(gid, "") < cause:
                out.append({"role": "designer", "gid": gid, "cause": cause})
        elif section == asana.READY and asana.allowed(t):
            cause = asana.human_activity(t)
            if attempted.get("developer", {}).get(gid, "") < cause:
                out.append({"role": "developer", "gid": gid, "cause": cause})
        elif section == asana.IN_REVIEW:
            cause = _comment_since(t, attempted.get("developer", {}).get(gid, ""))
            if cause:
                out.append({"role": "revise", "gid": gid, "cause": cause})
    out.sort(
        key=lambda j: asana.work_order(
            next(t for t in tasks if str(t["gid"]) == j["gid"])
        )
    )
    return out[:MAX_JOBS_PER_PASS]


def message(job: dict) -> dict:
    """The queue message for a job: the runtime payload plus FIFO group and dedupe ids."""
    role = "developer" if job["role"] in ("developer", "revise") else "designer"
    payload = {"role": role, "task_gid": job["gid"]}
    if job["role"] == "revise":
        payload["revise"] = True
    key = f"plan#{job['role']}#{job['gid']}#{job['cause']}"
    return {
        "body": payload,
        "group": f"{job['role']}-{job['gid']}",  # per task: different tasks run in parallel
        "dedupe_key": key,
    }
