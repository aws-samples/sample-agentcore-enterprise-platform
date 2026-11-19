"""Daily factory digest (role digest): one summary row per UTC day in the runs table. No model calls."""

import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Key

from factory.ci_fixer import Run

AGENTS = ("ci_fixer", "pr_reviewer", "product_manager", "developer", "merge_handler")
OPENED = {"fix_pr_opened", "pr_opened"}
# (label, agent whose spend counts, agent and outcome that count the units). Reviews are split below.
UNIT_COSTS = (
    ("per CI fix PR", "ci_fixer", "ci_fixer", "fix_pr_opened"),
    ("per merged dev PR", "developer", "merge_handler", "merged_recorded"),
)


def _review_bucket(r: dict) -> str:
    """posted | benchmark (the bench harness appends 'bench:<id>' to reason) | dry_run (a real PR, not posted)."""
    if r.get("outcome") == "review_posted":
        return "posted"
    return "benchmark" if "bench:" in str(r.get("reason", "")) else "dry_run"


def _spend(rows: list[dict]) -> float:
    return sum(float(r.get("est_cost_usd") or 0) for r in rows)


def _unit(spend: float, n: int) -> dict:
    return {
        "spend_usd": round(spend, 4),
        "count": n,
        "usd_each": round(spend / n, 4) if n else None,
    }


def unit_costs(rows_by_agent: dict[str, list[dict]]) -> dict:
    """Spend divided by outcome count per unit.

    Reviews are priced on posted reviews only. Dry runs on real PRs and benchmark runs are listed apart, so
    neither inflates the per-review figure. A posted review that only published a stored dry run costs $0 here;
    that dry run's spend shows on its own line.
    """
    reviews = defaultdict(list)
    for r in rows_by_agent.get("pr_reviewer", []):
        reviews[_review_bucket(r)].append(r)
    out = {
        "per posted review": _unit(_spend(reviews["posted"]), len(reviews["posted"])),
        "review dry runs on real PRs (not posted)": _unit(
            _spend(reviews["dry_run"]), len(reviews["dry_run"])
        ),
        "review benchmark runs (not per PR)": _unit(
            _spend(reviews["benchmark"]), len(reviews["benchmark"])
        ),
    }
    for label, spend_agent, count_agent, outcome in UNIT_COSTS:
        n = sum(
            1 for r in rows_by_agent.get(count_agent, []) if r.get("outcome") == outcome
        )
        out[label] = _unit(_spend(rows_by_agent.get(spend_agent, [])), n)
    return out


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
        day = datetime.fromisoformat(self.day)
        nxt = (day + timedelta(days=1)).date().isoformat()
        week = (
            (day - timedelta(days=6)).date().isoformat()
        )  # the 7 UTC days ending on self.day
        week_rows = self._rows(table, week, nxt)
        # sk starts with the run's UTC start time, so the first 10 characters are its day.
        day_rows = {
            a: [r for r in rs if str(r["sk"])[:10] == self.day]
            for a, rs in week_rows.items()
        }
        summary = summarize(day_rows, self.day)
        summary["unit_costs"] = {
            "1d": unit_costs(day_rows),
            "7d": unit_costs(week_rows),
        }
        return {
            "outcome": "digest_written",
            "summary": summary,
            "check_passed": True,
            "reason": f"{summary['runs']} runs, ${summary['cost_usd_total']}",
        }

    @staticmethod
    def _rows(table, start: str, end: str) -> dict[str, list[dict]]:
        rows = {}
        for agent in AGENTS:
            kwargs = {
                "KeyConditionExpression": Key("pk").eq(f"agent#{agent}")
                & Key("sk").between(start, end)
            }
            rows[agent] = []
            while True:
                page = table.query(**kwargs)
                rows[agent] += page["Items"]
                if "LastEvaluatedKey" not in page:
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return rows


def _unit_lines(unit: dict) -> list[str]:
    return [
        f"- {label}: "
        + (f"${v['usd_each']:.4f}" if v["usd_each"] is not None else "no outcomes")
        + f" (n={v['count']}, ${v['spend_usd']:.4f} spend)"
        for label, v in unit.items()
    ]


def render(summary: dict, pending: list[str], open_prs: list[int]) -> str:
    """The digest comment on the pinned 'Factory digest' issue: totals plus what waits on a human."""
    cost = ", ".join(f"{r} ${c:.4f}" for r, c in summary["cost_usd_by_role"].items())
    outcomes = "; ".join(f"{r}: {o}" for r, o in summary["outcomes_by_role"].items())
    return "\n".join(
        [
            f"Factory digest for {summary['day']} (UTC day)",
            f"Runs: {summary['runs']}. Cost: ${summary['cost_usd_total']:.4f} ({cost or 'none'}).",
            f"Outcomes by role: {outcomes or 'none'}",
            f"PRs opened: {', '.join(summary['prs_opened']) or 'none'}",
            f"PRs merged: {', '.join(summary['prs_merged']) or 'none'}",
            f"Failures: {len(summary['failures'])}",
            "",
            "Cost per outcome, last day:",
            *_unit_lines(summary["unit_costs"]["1d"]),
            "Cost per outcome, last 7 days:",
            *_unit_lines(summary["unit_costs"]["7d"]),
            "",
            "Waiting on you:",
            f"- Proposals to /approve or /reject: {', '.join(pending) or 'none'}",
            f"- Factory PRs open for review: {', '.join('#' + str(n) for n in open_prs) or 'none'}",
        ]
    )
