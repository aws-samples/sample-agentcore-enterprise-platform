"""Replay benchmark: review real merged PRs whose code a later PR fixed, and score the later fix.

    AWS_REGION=us-east-1 python factory/bench/replay.py --clone <full clone of the repo>

Ground truth (replay_cases.json) was mined with `git blame --first-parent` on the lines each follow-up PR
changed: those lines were introduced by the original PR, the follow-up landed within 14 days, and it is a
defect fix (checked by hand). The reviewer sees only the original PR's diff and description. A hit is a
blocker/major/minor finding in a ground-truth file within 5 lines of a line the follow-up changed.
Read-only on GitHub: patches come from the local clone, descriptions from `gh pr view`.
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "aws-samples/sample-agentcore-enterprise-platform"
RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:045129524125:runtime/software_factory-qVTpiwEjJ1"


def hit(truth: list[dict], findings: list[dict], slack: int = 5) -> list[dict]:
    return [
        f
        for f in findings
        if f.get("severity") in ("blocker", "major", "minor")
        and isinstance(f.get("line"), int)
        and any(
            f.get("path") == t["path"] and abs(f["line"] - t["line"]) <= slack
            for t in truth
        )
    ]


def main() -> int:
    import boto3
    from botocore.config import Config

    ap = argparse.ArgumentParser()
    ap.add_argument("--clone", required=True)
    a = ap.parse_args()
    cases = json.loads((HERE / "replay_cases.json").read_text())
    client = boto3.client(
        "bedrock-agentcore",
        config=Config(read_timeout=900, retries={"total_max_attempts": 1}),
    )
    stamp = int(time.time())

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=a.clone, capture_output=True, text=True, check=True
        ).stdout

    def run(c: dict) -> dict:
        pr = json.loads(
            subprocess.run(
                ["gh", "pr", "view", str(c["pr"]), "-R", REPO, "--json", "title,body"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        payload = {
            "role": "pr_reviewer",
            "wait": True,
            "bench": {
                "id": f"replay-{c['pr']}",
                "title": pr["title"],
                "body": pr.get("body") or "",
                "base": git("rev-parse", f"{c['merge']}^1").strip(),
                "patch": git("diff", "--binary", f"{c['merge']}^1", c["merge"]),
            },
        }
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME,
            qualifier="DEFAULT",
            runtimeSessionId=f"replay-{c['pr']}-{stamp}".ljust(40, "-"),
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode(),
        )
        out = json.loads(resp["response"].read())
        findings = out.get("findings") or []
        hits = hit(c["truth"], findings)
        others = [
            f
            for f in findings
            if f not in hits and f.get("severity") in ("blocker", "major")
        ]
        return {
            "pr": c["pr"],
            "fix_pr": c["fix_pr"],
            "outcome": out.get("outcome"),
            "reason": out.get("reason"),
            "cost": out.get("est_cost_usd"),
            "model": list(out.get("models") or {}),
            "verdict": out.get("verdict"),
            "hit": bool(hits),
            "hits": hits,
            "other_major": others,
            "findings": findings,
        }

    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(run, cases))
    (HERE / "replay_results.json").write_text(json.dumps(results, indent=1) + "\n")
    for r in results:
        print(
            f"#{r['pr']} -> #{r['fix_pr']}: {'HIT ' if r['hit'] else 'MISS'} ${r['cost']} {r['model']} "
            f"{r['verdict']} findings={len(r['findings'])} other_major={len(r['other_major'])} {r['outcome']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
