"""Reviewer benchmark: seeded-bug and clean cases, scored on precision / recall / F1 and cost.

    AWS_REGION=us-east-1 python factory/bench/run_bench.py [--runtime-arn ARN] [--base SHA]

Each case is a patch on BASE (the repo commit the cases were cut from). The runtime applies it in its own
clone, so nothing is pushed to GitHub. Scoring: a seeded bug is caught (TP) when a blocker/major/minor
finding names the same file, within 5 lines, in one of the expected categories. Every other blocker/major
finding is a false positive (FP); an uncaught bug is a false negative (FN).
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "ad346d8fb9f3f78ead9022dae79ab06edc0280fa"
RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:045129524125:runtime/software_factory-qVTpiwEjJ1"
LINE_SLACK = 5


def score(expected: list[dict], findings: list[dict]) -> dict:
    """Match findings to seeded bugs. Each bug and each finding is used at most once."""
    used: set[int] = set()
    caught, missed = [], []
    for bug in expected:
        hit = next(
            (
                i
                for i, f in enumerate(findings)
                if i not in used
                and f.get("severity") in ("blocker", "major", "minor")
                and f.get("path") == bug["path"]
                and isinstance(f.get("line"), int)
                and abs(f["line"] - bug["line"]) <= LINE_SLACK
                and f.get("category") in bug["categories"]
            ),
            None,
        )
        if hit is None:
            missed.append(bug["bug"])
        else:
            used.add(hit)
            caught.append(bug["bug"])
    fp = [
        f
        for i, f in enumerate(findings)
        if i not in used and f.get("severity") in ("blocker", "major")
    ]
    return {
        "tp": len(caught),
        "fn": len(missed),
        "fp": len(fp),
        "missed": missed,
        "false_positives": [
            f"{f.get('path')}:{f.get('line')} {f.get('title')}" for f in fp
        ],
    }


def totals(scores: list[dict]) -> dict:
    tp, fp, fn = (sum(s[k] for s in scores) for k in ("tp", "fp", "fn"))
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(p, 3),
        "recall": round(r, 3),
        "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
    }


def main() -> int:
    import boto3
    from botocore.config import Config

    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-arn", default=RUNTIME)
    ap.add_argument("--base", default=BASE)
    a = ap.parse_args()
    cases = json.loads((HERE / "cases.json").read_text())
    stamp = int(
        time.time()
    )  # fresh sessions: a reused session id reattaches to a microVM on the old version
    client = boto3.client(
        "bedrock-agentcore",
        config=Config(read_timeout=900, retries={"total_max_attempts": 1}),
    )

    def run(case: dict) -> dict:
        payload = {
            "role": "pr_reviewer",
            "wait": True,
            "bench": {
                "id": case["id"],
                "title": case["title"],
                "body": case["body"],
                "base": a.base,
                "patch": (HERE / "cases" / f"{case['id']}.patch").read_text(),
            },
        }
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=a.runtime_arn,
            qualifier="DEFAULT",
            runtimeSessionId=f"bench-{case['id']}-{stamp}".ljust(40, "-"),
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode(),
        )
        out = json.loads(resp["response"].read())
        return {
            "id": case["id"],
            "difficulty": case["difficulty"],
            "outcome": out.get("outcome"),
            "reason": out.get("reason"),
            "cost": out.get("est_cost_usd"),
            "models": out.get("models"),
            "model_reason": out.get("model_reason"),
            "verdict": out.get("verdict"),
            "findings": out.get("findings") or [],
            **score(case["expected"], out.get("findings") or []),
        }

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(run, cases))
    report = {
        "cases": results,
        "totals": totals(results),
        "cost_usd": round(sum(r["cost"] or 0 for r in results), 4),
    }
    (HERE / "results.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    for r in results:
        print(
            f"{r['id']:28} {r['outcome']:17} tp={r['tp']} fp={r['fp']} fn={r['fn']} ${r['cost']} {list(r['models'] or {})}"
        )
    print(json.dumps(report["totals"]), "total cost", report["cost_usd"])
    return 0 if all(r["outcome"] == "reviewed_dry_run" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
