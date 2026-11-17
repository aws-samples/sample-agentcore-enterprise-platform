"""Daily factory digest (role digest): one summary row per UTC day in the runs table. No model calls."""

import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Key

from factory.ci_fixer import Run

AGENTS = ("ci_fixer", "pr_reviewer", "product_manager", "developer", "merge_handler")
OPENED = {"fix_pr_opened", "pr_opened"}


def summarize(rows_by_agent: dict[str, list[dict]], day: str) -> dict:
    cost, outcomes, failures, opened, merged = defaultdict(float), {}, [], [], []
    for agent, rows in rows_by_agent.items():
        outcomes[agent] = dict(Counter(r.get("outcome", "?") for r in rows))
        for r in rows:
            cost[agent] += float(r.get("est_cost_usd") or 0)
            if r.get("outcome") == "failed":
                failures.append(
                    {
                        "agent": agent,
                        "trigger": r.get("trigger"),
                        "reason": str(r.get("reason", ""))[:200],
                    }
                )
            if r.get("outcome") in OPENED and r.get("pr_url"):
                opened.append(r["pr_url"])
            if agent == "merge_handler" and r.get("pr_url"):
                merged.append(r["pr_url"])
    return {
        "day": day,
        "runs": sum(len(r) for r in rows_by_agent.values()),
        "cost_usd_by_role": {k: round(v, 4) for k, v in cost.items()},
        "cost_usd_total": round(sum(cost.values()), 4),
        "outcomes_by_role": outcomes,
        "prs_opened": opened,
        "prs_merged": merged,
        "failures": failures[:20],
    }


class Digest(Run):
    AGENT = "digest"
    EXTRA_KEYS = ("summary",)

    def __init__(self, work, region: str, repo_name: str, day: str | None = None):
        day = (
            day or (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        )  # yesterday, a full day
        super().__init__(
            {
                "id": f"digest-{day}",
                "repository": {"full_name": repo_name},
                "name": "digest",
            },
            work,
            work,
            None,
            region,
        )
        self.day, self.branch = day, ""

    def execute(self) -> dict:
        table = self.session.resource("dynamodb").Table(
            os.environ["FACTORY_RUNS_TABLE"]
        )
        nxt = (datetime.fromisoformat(self.day) + timedelta(days=1)).date().isoformat()
        rows = {}
        for agent in AGENTS:
            kwargs = {
                "KeyConditionExpression": Key("pk").eq(f"agent#{agent}")
                & Key("sk").between(self.day, nxt)
            }
            rows[agent] = []
            while True:
                page = table.query(**kwargs)
                rows[agent] += page["Items"]
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        summary = summarize(rows, self.day)
        return {
            "outcome": "digest_written",
            "summary": summary,
            "check_passed": True,
            "reason": f"{summary['runs']} runs, ${summary['cost_usd_total']}",
        }
