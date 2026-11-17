"""Entrypoint: FACTORY_ROLE=ci_fixer python -m factory --event run.json --log job.log --repo <clone> --work <dir>"""

import argparse
import json
import os
import sys
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError


def main() -> int:
    role = os.environ.get("FACTORY_ROLE")
    if role != "ci_fixer":
        print(f"unsupported FACTORY_ROLE={role!r} (only ci_fixer)", file=sys.stderr)
        return 2
    from factory.ci_fixer import Run

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--event", required=True, help="workflow_run webhook payload or run object JSON"
    )
    ap.add_argument("--log", required=True, help="failed job log text")
    ap.add_argument(
        "--repo", required=True, help="local clone (read-only source for the worktree)"
    )
    ap.add_argument(
        "--work",
        required=True,
        help="scratch dir for worktrees, caches, factory-runs.jsonl",
    )
    ap.add_argument("--profile", default=os.environ.get("FACTORY_AWS_PROFILE"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    a = ap.parse_args()

    event = json.loads(Path(a.event).read_text())
    run_obj = event.get("workflow_run", event)
    if run_obj.get("conclusion") != "failure":
        print("run did not fail; nothing to do")
        return 0
    if run_obj.get("head_branch", "").startswith("factory/ci-fix/"):
        print("refusing to fix a factory fix branch (no fix-of-fix loops)")
        return 0

    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    run = Run(run_obj, Path(a.repo).resolve(), work, a.profile, a.region)
    try:
        rec = run.execute(Path(a.log).read_text(errors="replace"))
    except (ClientError, BotoCoreError) as e:
        rec = {"outcome": "failed", "reason": f"aws: {e}", "check_passed": False}
    usage = run.record(rec)

    print(
        f"== triage: {rec.get('triage', '')}\n== outcome: {rec['outcome']} {rec.get('reason', '')}"
    )
    print(f"== check passed: {rec['check_passed']}\n{rec.get('check_output', '')}")
    for n in rec.get("apply_notes", []):
        print(f"== note: {n}")
    if rec.get("diff"):
        print(f"== diff (branch {run.branch}):\n{rec['diff']}")
    if rec["outcome"] == "fix_ready_dry_run":
        print(
            f"== DRY RUN: would open PR {run.branch} -> main\nTitle: {rec['pr_title']}\n\n{rec['pr_body']}"
        )
    print(f"== usage: {json.dumps(usage)}")
    return 0 if rec["check_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
