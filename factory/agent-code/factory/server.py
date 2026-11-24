"""AgentCore Runtime HTTP entrypoint: GET /ping, POST /invocations on port 8080.

Payload: {"role": "ci_fixer", "workflow_run": {...}, "log_text": optional, "wait": optional}.
Without "wait" the run starts in a background thread and the call returns at once;
/ping reports HealthyBusy until it ends, which keeps the session alive (design 2.2).
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3

from factory import asana, ci_fixer, config, github, issues, untrusted
from factory.ci_fixer import Run
from factory.consult import Consult
from factory.designer import Designer, attempted_designs, next_design
from factory.developer import (
    Developer,
    MergeHandler,
    Reviser,
    attempted_tasks,
    next_asana_task,
    next_task,
    revision_requests,
    taken_tasks,
)
from factory.digest import Digest
from factory.digest import render as render_digest
from factory.pm import ProductManager
from factory.reviewer import Review, build_review

REPO = config.REPO
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_BODY = 5 * 1024 * 1024
# Builds run in parallel now; this bounds how many factory PRs wait for a human at once.
MAX_OPEN_FACTORY_PRS = int(os.environ.get("FACTORY_MAX_OPEN_PRS", "5"))
# Issue-comment commands. Kept in step with ingress.py's own parser, which is only one of the
# producers that reach validate(); the names are what handle_issue_command dispatches on.
ISSUE_COMMANDS = {"pm", "dev", "ready", "approve", "reject"}
MAX_COMMAND_TEXT = 4000  # ingress.MAX_PM_TEXT
_PROPOSAL_IDS = re.compile(r"(all|P-[0-9a-f]{6})( P-[0-9a-f]{6})*")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")
# Each role's GitHub writes need the switch its config row names, on top of the App secret.
# A role with no config row cannot publish: config.publish fails closed (see config.py).
PUBLISH = config.publish("ci_fixer")
REVIEW_PUBLISH = config.publish("pr_reviewer")
DEV_PUBLISH = config.publish("developer")
PM_PUBLISH = config.publish("product_manager")
_busy = threading.Lock()  # one run per session; each session is its own microVM
_state = {"last_update": int(time.time())}


def load_app_creds() -> dict | None:
    """The App secret is '{}' until the user creates the GitHub App; None means dry run."""
    name = os.environ.get("FACTORY_GITHUB_APP_SECRET")
    if not name:
        return None
    raw = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
        SecretId=name
    )["SecretString"]
    creds = json.loads(raw or "{}")
    if (
        creds.get("app_id")
        and creds.get("installation_id")
        and creds.get("private_key_pem")
    ):
        return creds
    return None


def verify_app() -> dict:
    """Read-only GitHub App check; never returns or logs the secret or the token."""
    name = os.environ.get("FACTORY_GITHUB_APP_SECRET", "")
    raw = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
        SecretId=name
    )["SecretString"]
    try:
        creds = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "secret is not valid JSON"}
    absent = [
        k for k in ("app_id", "installation_id", "private_key_pem") if not creds.get(k)
    ]
    if absent or not isinstance(creds, dict):
        return {"ok": False, "error": f"secret JSON lacks {absent}"}
    try:
        return {"ok": True, **github.verify_app(creds, REPO)}
    except (
        RuntimeError,
        ValueError,
        TypeError,
    ) as e:  # GitHub errors carry status + GitHub's message only
        return {"ok": False, "error": str(e)[:400]}


def validate(payload: dict) -> dict:
    """Trust boundary: the payload comes from the dispatcher or a human invoke."""
    if payload.get("role") == "verify_app":
        return {}
    if payload.get("role") == "pr_reviewer" and "bench" in payload:
        b = payload["bench"]
        if not re.fullmatch(r"[0-9a-f]{40}", str(b.get("base", ""))) or not isinstance(
            b.get("patch"), str
        ):
            raise ValueError("bench needs a 40-hex base sha and a patch string")
        if len(b["patch"]) > 300_000 or not re.fullmatch(
            r"[\w.-]{1,64}", str(b.get("id", ""))
        ):
            raise ValueError("bench patch too large or bad id")
        return {"id": str(b["id"])}
    if payload.get("role") == "pr_reviewer":
        number = int(payload["pull_request"]["number"])
        if number <= 0:
            raise ValueError("pull_request.number must be positive")
        if payload.get("publish"):
            if not re.fullmatch(
                r"\d{4}-\d\d-\d\dT[\d:.+]+#[0-9a-f]{12}",
                str(payload.get("from_run_sk", "")),
            ):
                raise ValueError(
                    "publish needs from_run_sk: the runs-table sort key of the stored review"
                )
            extras = payload.get("extra_findings") or []
            if not isinstance(extras, list) or len(extras) > 10:
                raise ValueError("extra_findings must be a list of at most 10")
            for f in extras:
                if not (
                    isinstance(f, dict)
                    and isinstance(f.get("path"), str)
                    and isinstance(f.get("line"), int)
                    and f.get("severity") in ("blocker", "major", "minor", "nit")
                    and isinstance(f.get("title"), str)
                    and 0 < len(f["title"]) <= 200
                    and isinstance(f.get("body"), str)
                    and 0 < len(f["body"]) <= 3000
                ):
                    raise ValueError(
                        "each extra finding needs path, int line, severity, title, body"
                    )
            if (
                not isinstance(payload.get("summary", ""), str)
                or len(payload.get("summary", "")) > 4000
            ):
                raise ValueError("summary must be a string of at most 4000 characters")
        cap = payload.get("cost_cap_usd")
        if cap is not None and not (isinstance(cap, (int, float)) and 0 < cap <= 2.0):
            raise ValueError("cost_cap_usd must be a number in (0, 2.0]")
        paths = payload.get("paths")
        if paths is not None:
            if not isinstance(paths, list) or not 1 <= len(paths) <= 50:
                raise ValueError("paths must be a list of 1 to 50 repository paths")
            for p in paths:
                if (
                    not isinstance(p, str)
                    or not re.fullmatch(r"[\w.][\w.\-/]{0,199}", p)
                    or ".." in p
                ):
                    raise ValueError(
                        "each path must be a relative repository path, no '..'"
                    )
        note = payload.get("note")
        if note is not None and not (isinstance(note, str) and 0 < len(note) <= 1000):
            raise ValueError("note must be a string of at most 1000 characters")
        return {"id": number}
    if payload.get("role") == "product_manager" and payload.get("mode") == "consult":
        if not re.fullmatch(r"[\w-]{8,64}", str(payload.get("conversation_id", ""))):
            raise ValueError("conversation_id must be [A-Za-z0-9_-]{8,64}")
        if (
            not isinstance(payload.get("message"), str)
            or not 0 < len(payload["message"]) <= 8000
        ):
            raise ValueError("message must be a string of 1-8000 characters")
        return {"id": f"consult-{payload['conversation_id']}"}
    if payload.get("role") == "product_manager":
        rulings = payload.get("rulings") or []
        if (
            not isinstance(rulings, list)
            or len(rulings) > 50
            or not all(isinstance(r, dict) for r in rulings)
        ):
            raise ValueError("rulings must be a list of at most 50 objects")
        return {"id": "pm"}
    if payload.get("role") in ("developer", "designer") and "task_gid" in payload:
        gid = str(payload["task_gid"])
        if not re.fullmatch(r"\d{1,20}", gid):
            raise ValueError("task_gid must be a numeric board task id")
        if payload.get("revise") not in (None, True, False):
            raise ValueError("revise must be a boolean")
        if payload.get("from_review") not in (None, True, False):
            raise ValueError("from_review must be a boolean")
        prefix = "design" if payload["role"] == "designer" else "task"
        return {"id": f"{prefix}-{gid}"}
    if payload.get("role") == "developer":
        task = payload.get("task")
        if task is not None and not (
            re.fullmatch(r"\d{1,20}", str(task.get("gid", "")))
            and str(task.get("title", "")).strip()
        ):
            raise ValueError("task needs a numeric gid and a title")
        return {"id": f"task-{(task or {}).get('gid', 'next')}"}
    if payload.get("role") == "designer":
        return {"id": "design-next"}
    if payload.get("role") == "digest":
        day = payload.get("day")
        if day is not None and not re.fullmatch(r"\d{4}-\d\d-\d\d", str(day)):
            raise ValueError("day must be YYYY-MM-DD")
        return {"id": f"digest-{day or 'yesterday'}"}
    if payload.get("role") == "merge_handler":
        number = int(payload["pull_request"]["number"])
        if number <= 0:
            raise ValueError("pull_request.number must be positive")
        return {"id": f"merge-{number}"}
    if payload.get("role") == "issue_command":
        # The ingress parses these, but it is one producer among several (a human invoke is
        # another), so every field the handler reads is re-checked here independently.
        name = payload.get("command")
        if name not in ISSUE_COMMANDS:
            raise ValueError(f"command must be one of {sorted(ISSUE_COMMANDS)}")
        number = int(payload["issue"]["number"])
        if number <= 0:
            raise ValueError("issue.number must be positive")
        login = str((payload.get("actor") or {}).get("login", ""))
        if not _LOGIN.fullmatch(login):
            raise ValueError("actor.login must be a GitHub login")
        args = payload.get("args", "")
        if not isinstance(args, str) or len(args) > MAX_COMMAND_TEXT:
            raise ValueError(f"args must be a string of at most {MAX_COMMAND_TEXT}")
        if name == "pm" and not args.strip():
            raise ValueError("/pm needs a question")
        if name in ("approve", "reject"):
            if not _PROPOSAL_IDS.fullmatch(args):
                raise ValueError("approve/reject take 'all' or P-xxxxxx ids")
            if name == "reject" and args == "all":
                raise ValueError("/reject all is refused: reject proposals by id")
        if payload.get("repository", {}).get("full_name", REPO) != REPO:
            raise ValueError("command is not from the configured repository")
        return {"id": f"{name}-{number}"}
    if payload.get("role") != "ci_fixer":
        raise ValueError(f"unsupported role {payload.get('role')!r}")
    run = payload["workflow_run"]
    run["id"] = int(run["id"])
    if run.get("repository", {}).get("full_name", REPO) != REPO:
        raise ValueError("run is not from the configured repository")
    if not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", run.get("path", "")):
        raise ValueError("workflow path must be .github/workflows/<file>.yml")
    if run.get("conclusion") != "failure" or str(run.get("head_branch", "")).startswith(
        "factory/"
    ):
        raise ValueError("only failed runs on non-factory branches are fixed")
    run.setdefault("repository", {"full_name": REPO})
    return run


# What each role's GitHub writes need, as installation_token() names them. A role absent here
# needs no permission of its own, and is never skipped by the pre-flight.
ROLE_PERMISSIONS = {
    "pr_reviewer": ("pull_requests:write",),
    "product_manager": ("issues:write",),
    "developer": (
        "issues:write",
        "contents:write",
    ),  # labels and comments, then the push
    "ci_fixer": ("actions:read", "contents:write"),
}


# In Asana mode the board is written through the outbox, so no role needs the Issues permission;
# the developer still pushes, and the designer never touches GitHub.
ASANA_ROLE_PERMISSIONS = {
    "pr_reviewer": ("pull_requests:write",),
    "developer": ("contents:write",),
    "ci_fixer": ("actions:read", "contents:write"),
}
# The roles that read the board. A missing or stale snapshot skips them before any clone or model call.
ASANA_ROLES = {"product_manager", "developer", "designer"}


def preflight(role: str) -> dict | None:
    """Is the repo reachable, and does the installation grant what this role writes?

    Runs before clone() and before any model call, so a run that cannot finish costs no tokens.
    Returns a skip record, or None to proceed. Without App creds the run is a dry run reading the
    public repo anonymously, which needs no permission, so there is nothing to check.
    """
    if asana.enabled() and role in ASANA_ROLES:
        try:
            asana.load_snapshot(boto3.Session(region_name=REGION))
        except asana.StaleSnapshot as e:
            return {
                "outcome": "skipped_stale_snapshot",
                "reason": f"stale_snapshot: {e}",
            }
    need = (ASANA_ROLE_PERMISSIONS if asana.enabled() else ROLE_PERMISSIONS).get(role)
    if not need:
        return None
    try:
        creds = load_app_creds()
        if not creds:
            return None
        token, missing = github.installation_token(creds, REPO)
        github.request("GET", f"/repos/{REPO}", token)
    except Exception as e:  # noqa: BLE001 - any failure here means we must not start the run
        return {
            "outcome": "skipped_repo_unreachable",
            "reason": f"repo_unreachable: {type(e).__name__}: {e}"[:300],
        }
    lacks = [p for p in need if p in missing]
    if lacks:
        # Its own outcome, not plain "skipped": the digest counts outcomes per role, so this
        # becomes a line of its own instead of blending into ordinary skips.
        return {
            "outcome": "skipped_missing_permission",
            "reason": "missing_permission:" + ",".join(lacks),
        }
    return None


def _record_row(role: str, trigger: str, rec: dict) -> dict:
    """A run row for a run no agent recorded: a pre-flight skip, an early return, or a raise.

    Same table and shape, with no clone and no model call. The handler's own fields are kept in the
    reply; only what Run.EXTRA_KEYS names reaches the row, which is enough to count the outcome.
    """
    work = Path("/tmp/factory") / "unrecorded"
    work.mkdir(parents=True, exist_ok=True)
    agent = Run(
        {"id": trigger, "repository": {"full_name": REPO}}, work, work, None, REGION
    )
    agent.AGENT, agent.branch = role, ""
    # An outcome is what makes a row countable; a reply carrying none is itself a reporting failure.
    return {**rec, **_finish(agent, {"outcome": "failed", **rec}, True, ())}


def clone(work: Path) -> Path:
    repo = work / "repo"
    if not repo.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--filter=blob:none",
                f"https://github.com/{REPO}.git",
                str(repo),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
    return repo


def handle_bench(payload: dict) -> dict:
    """Benchmark: review base + a local patch. Never touches GitHub beyond cloning the public repo."""
    b = payload["bench"]
    work = Path("/tmp/factory") / f"bench-{b['id']}"
    pr = {
        "number": 0,
        "title": f"BENCH {b['id']}: {b.get('title', '')}"[:200],
        "body": b.get("body", ""),
        "user": {"login": "bench-author"},
        "head": {"sha": "", "ref": f"bench/{b['id']}"},
        "base": {"sha": b["base"], "repo": {"full_name": REPO}},
    }
    agent = Review(pr, clone(work), work, REGION, patch=b["patch"])
    try:
        rec = agent.execute()
    except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
        traceback.print_exc()
        rec = {"outcome": "failed", "reason": f"{type(e).__name__}: {e}"[:500]}
    rec["reason"] = (rec.get("reason", "") + f" bench:{b['id']}").strip()
    line = agent.record(rec, dry_run=True)
    keys = ("review", "verdict", "summary", "model_reason", "files_skipped", "findings")
    return {**line, **{k: rec[k] for k in keys if rec.get(k)}}


def queue_autofix(pr: dict) -> dict:
    """One automatic revision round for the reviewer's major/blocker findings on a factory task PR.

    Once per PR (a dedupe claim), so the reviewer and the reviser cannot loop: the review of the
    fix-up commit goes to the maintainer, whatever it says. Minor findings are never auto-fixed.
    """
    import re as _re

    m = _re.fullmatch(r"factory/task/(\d+)", str((pr.get("head") or {}).get("ref", "")))
    queue, dedupe = (
        os.environ.get("FACTORY_QUEUE_URL"),
        os.environ.get("FACTORY_DEDUPE_TABLE"),
    )
    if not (asana.enabled() and m and queue and dedupe):
        return {"autofix": "not_applicable"}
    table = boto3.resource("dynamodb", region_name=REGION).Table(dedupe)
    try:
        table.put_item(
            Item={
                "delivery_id": f"autofix#{pr['number']}",
                "expires_at": int(time.time()) + 30 * 86400,
            },
            ConditionExpression="attribute_not_exists(delivery_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return {"autofix": "round_already_used"}
    gid = m.group(1)
    boto3.client("sqs", region_name=REGION).send_message(
        QueueUrl=queue,
        MessageBody=json.dumps(
            {"role": "developer", "task_gid": gid, "revise": True, "from_review": True}
        ),
        MessageGroupId=f"revise-{gid}",
        MessageDeduplicationId=f"autofix-{pr['number']}",
    )
    return {"autofix": "queued", "task_gid": gid}


def blocking_findings(texts: list[str]) -> list[str]:
    """The major and blocker findings in a review's text (one per inline comment)."""
    out = []
    for block in "\n\n".join(texts).split("\n\n"):
        lines = block.strip().splitlines()
        head = next((ln for ln in lines if ln.startswith("**")), "")
        if head.lower().startswith(("**major", "**blocker")):
            out.append(" ".join(ln.strip() for ln in lines)[:1500])
    return out


def as_own_pr(review: dict, pr: dict) -> dict:
    """GitHub refuses REQUEST_CHANGES from the PR's own author, and the factory's App opens its
    own PRs. There the verdict becomes a COMMENT that says so at the top, so it is still seen."""
    user = pr.get("user") or {}
    if review.get("event") != "REQUEST_CHANGES" or user.get("type") != "Bot":
        return review
    head = "**Changes requested** (posted as a comment: GitHub does not let an app request changes on its own PR)."
    return {**review, "event": "COMMENT", "body": f"{head}\n\n{review.get('body', '')}"}


def handle_review(payload: dict) -> dict:
    """Review a PR from its diff only. Dry run unless FACTORY_REVIEW_PUBLISH=true."""
    if "bench" in payload:
        return handle_bench(payload)
    number = int(payload["pull_request"]["number"])
    creds = load_app_creds()
    token = github.installation_token(creds, REPO)[0] if creds else ""
    pr = github.get_pull(REPO, number, token)
    if pr["base"]["repo"]["full_name"] != REPO:
        raise ValueError("PR is not against the configured repository")
    agent = Review(
        pr,
        clone(Path("/tmp/factory") / f"pr-{number}"),
        Path("/tmp/factory") / f"pr-{number}",
        REGION,
    )
    if payload.get(
        "paths"
    ):  # opt-in narrowing: review only these changed paths, nothing else
        agent.paths = list(payload["paths"])
    if payload.get("note"):  # operator context, appended to the posted review body
        agent.note = str(payload["note"])
    if payload.get(
        "cost_cap_usd"
    ):  # one-off large review: the dollar cap governs, so lift the token cap with it
        agent.cost_cap = float(payload["cost_cap_usd"])
        agent.TOKEN_CAP = 1_200_000
    if payload.get("publish"):
        # One-off, explicitly requested publish of a stored review; the global switch stays off.
        rec = _safe(lambda: publish_stored(agent, pr, token, payload))
        return _finish(agent, rec, False, ("pr_url", "review", "source_run"))
    if pr["user"]["login"] == "dependabot[bot]":
        rec = {"outcome": "skipped", "reason": "dependabot PR"}
    else:
        try:
            rec = agent.execute()
            if token and REVIEW_PUBLISH and rec["outcome"] == "reviewed_dry_run":
                review = as_own_pr(rec["review"], pr)
                rec.update(
                    outcome="review_posted",
                    pr_url=github.post_review(REPO, number, token, review),
                )
                if rec["review"].get("event") == "REQUEST_CHANGES":
                    rec["autofix"] = _safe(lambda: queue_autofix(pr))
        except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
            traceback.print_exc()
            rec = {"outcome": "failed", "reason": f"{type(e).__name__}: {e}"[:500]}
    if asana.enabled() and rec.get("outcome") in ("review_posted", "reviewed_dry_run"):
        verdict = rec.get("verdict") or "reviewed"
        rec.update(
            _safe(
                lambda: board_link(
                    pr,
                    token,
                    bool(token and REVIEW_PUBLISH),
                    "pr_reviewer",
                    asana.IN_REVIEW,
                    f"Review of PR #{number} ({verdict}): {str(rec.get('summary') or '')[:800]}\n{pr['html_url']}",
                )
            )
        )
    line = agent.record(rec, dry_run=not (token and REVIEW_PUBLISH))
    print(json.dumps({"factory_run": line}), flush=True)
    return {
        **line,
        **{
            k: rec[k]
            for k in (
                "review",
                "verdict",
                "summary",
                "model_reason",
                "files_skipped",
                "projected_usd",
                "pr_url",
            )
            if rec.get(k)
        },
    }


def publish_stored(agent: Review, pr: dict, token: str, payload: dict) -> dict:
    """Post a review stored by an earlier dry run, plus human-added findings, as one COMMENT review."""
    if not token:
        return {"outcome": "failed", "reason": "no GitHub App token"}
    from boto3.dynamodb.conditions import Key

    table = boto3.resource("dynamodb", region_name=REGION).Table(
        os.environ["FACTORY_RUNS_TABLE"]
    )
    items = table.query(
        KeyConditionExpression=Key("pk").eq("agent#pr_reviewer")
        & Key("sk").eq(payload["from_run_sk"])
    )["Items"]
    if not items:
        return {"outcome": "failed", "reason": "stored review not found"}
    item = items[0]
    if item.get("trigger") != str(pr["number"]) or item.get("ref") != pr["head"]["sha"]:
        return {
            "outcome": "failed",
            "reason": "stored review is for another PR or an older head; re-review first",
        }
    extras = [
        f | {"title": f"(not from the reviewer agent) {f['title']}", "suggestion": ""}
        for f in payload.get("extra_findings") or []
    ]
    result = {
        "summary": payload.get("summary") or item.get("summary", ""),
        "verdict": item.get("verdict", ""),
        "findings": json.loads(item.get("findings") or "[]") + extras,
    }
    head, files = agent.diff_files()
    review = build_review(
        result,
        files,
        head,
        "+".join(item.get("models") or {}),
        [],
        covered=agent.paths,
        note=agent.note,
    )
    review["event"] = (
        "COMMENT"  # posting was approved as a comment: never APPROVE or REQUEST_CHANGES
    )
    url = github.post_review(REPO, int(pr["number"]), token, review)
    return {
        "outcome": "review_posted",
        "pr_url": url,
        "review": review,
        "source_run": f"{item['sk']} (review cost ${item.get('est_cost_usd')})",
        "check_passed": True,
    }


def _github_token() -> str:
    creds = load_app_creds()
    return github.installation_token(creds, REPO)[0] if creds else ""


def _finish(agent, rec: dict, dry_run: bool, keys: tuple) -> dict:
    line = agent.record(rec, dry_run=dry_run)
    print(json.dumps({"factory_run": line}), flush=True)
    return {**line, **{k: rec[k] for k in keys if rec.get(k)}}


def _safe(fn) -> dict:
    """Catch so the run still produces a row -- and record what was caught.

    Some callers store this result in a sub-field (the digest's digest_post) instead of making it
    the record, which used to erase the failure entirely. Appending here, rather than at each call
    site, means no caller can drop the evidence: record() writes it to the row as swallowed_errors.
    """
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
        traceback.print_exc()
        reason = f"{type(e).__name__}: {e}"[:500]
        ci_fixer.SWALLOWED.append(reason)
        return {"outcome": "failed", "reason": reason}


def handle_consult(payload: dict) -> dict:
    work = Path("/tmp/factory") / "pm"
    agent = Consult(
        clone(work),
        work,
        REGION,
        REPO,
        _github_token(),
        payload["conversation_id"],
        payload["message"],
        PM_PUBLISH,
    )
    rec = _safe(agent.execute)
    return _finish(
        agent,
        rec,
        not PM_PUBLISH,
        ("answer", "conversation_id", "turn", "queued", "issue_ops"),
    )


def handle_pm(payload: dict) -> dict:
    if payload.get("mode") == "consult":
        return handle_consult(payload)
    work = Path("/tmp/factory") / "pm"
    agent = ProductManager(clone(work), work, REGION, REPO, _github_token(), PM_PUBLISH)
    rec = _safe(
        lambda: agent.execute(
            initial=payload.get("initial"), rulings=payload.get("rulings")
        )
    )
    return _finish(
        agent,
        rec,
        not PM_PUBLISH,
        ("board_diff", "proposed_tasks", "reconcile", "dropped", "issue_ops"),
    )


def next_revision(tasks: list[dict], open_prs: list[dict], tried: dict) -> tuple | None:
    """(task, pr, requests) for the task in review with the oldest unanswered maintainer comment."""
    best = None
    for t in asana.in_section(tasks, asana.IN_REVIEW):
        pr = next(
            (p for p in open_prs if p["branch"] == f"factory/task/{t['gid']}"), None
        )
        asks = revision_requests(t, tried.get(str(t["gid"]), "")) if pr else []
        if asks and (best is None or asana.work_order(t) < asana.work_order(best[0])):
            best = (t, pr, asks)
    return best


def pr_review_context(number: int, token: str) -> tuple[list[str], list[str]]:
    """The latest review's summary and inline findings (untrusted text), and the paths those
    findings point at. Older reviews are left out: their findings are usually addressed."""
    reviews = [
        r
        for r in github.request("GET", f"/repos/{REPO}/pulls/{number}/reviews", token)
        if r.get("body")
    ]
    if not reviews:
        return [], []
    last = reviews[-1]
    inline = [
        c
        for c in github.request(
            "GET", f"/repos/{REPO}/pulls/{number}/comments?per_page=100", token
        )
        if c.get("pull_request_review_id") == last.get("id")
    ]
    text = str(last.get("body") or "") + "".join(
        f"\n\n{c.get('path')}:{c.get('line') or c.get('original_line')}\n{c.get('body')}"
        for c in inline
    )
    return [untrusted.clean(text, 12000, tail=False)], sorted(
        {str(c.get("path")) for c in inline if c.get("path")}
    )


def handle_revision(
    session, token: str, open_prs: list[dict], payload: dict
) -> dict | None:
    """A follow-up commit on a factory PR for the comments on its task; None when there are none."""
    tasks = asana.load_snapshot(session)["tasks"]
    if payload.get("task_gid"):
        tasks = [t for t in tasks if str(t["gid"]) == str(payload["task_gid"])]
    if payload.get("from_review"):
        # The automatic round: the requests are the reviewer's own major/blocker findings.
        t = tasks[0] if tasks else None
        pr_row = t and next(
            (p for p in open_prs if p["branch"] == f"factory/task/{t['gid']}"), None
        )
        if not pr_row:
            return None
        context, _ = pr_review_context(int(pr_row["number"]), token)
        asks = blocking_findings(context)
        if not asks:
            return None
        pick = (t, pr_row, asks)
    else:
        pick = next_revision(tasks, open_prs, attempted_tasks(session))
    if pick is None:
        return None
    t, pr_row, asks = pick
    full = github.get_pull(REPO, int(pr_row["number"]), token)
    reviews, finding_paths = pr_review_context(int(pr_row["number"]), token)
    pr = {
        "number": pr_row["number"],
        "head_sha": full["head"]["sha"],
        "files": pr_row["files"],
        "url": full["html_url"],
        "finding_paths": finding_paths,
    }
    publish = bool(DEV_PUBLISH and token and not payload.get("dry_run"))
    task = asana.developer_task(t)
    tracker = asana.Board(
        session, "developer", f"task-{t['gid']}-{int(time.time())}", publish
    )
    work = Path("/tmp/factory") / f"task-{t['gid']}"
    agent = Reviser(task, pr, asks, reviews, clone(work), work, REGION, REPO, tracker)
    rec = _safe(agent.execute)
    if publish and rec.get("outcome") == "revision_ready_dry_run":
        rec.update(_safe(lambda: agent.ship(token)))
    rec["issue_ops"] = tracker.ops
    keys = (
        "checks",
        "pr_title",
        "pr_body",
        "pr_url",
        "files_changed",
        "refused",
        "issue_ops",
    )
    return _finish(agent, rec, not publish, keys)


def handle_developer(payload: dict) -> dict:
    token = _github_token()
    session = boto3.Session(region_name=REGION)
    task = payload.get("task")
    open_prs = github.open_factory_prs(REPO, token)
    taken = taken_tasks(open_prs, github.factory_task_branches(REPO, token))
    duplicate = None
    if task is None and asana.enabled() and payload.get("task_gid"):
        # The planner named one task. A revision, or a build if it is (still) ready.
        if payload.get("revise"):
            rev = handle_revision(session, token, open_prs, payload)
            return rev or {
                "outcome": "skipped",
                "reason": "no unanswered comment on an open PR",
            }
        snap = asana.load_snapshot(session)["tasks"]
        # Ready for agent, or left in progress by an attempt that failed (no PR or branch for it).
        ready = asana.in_section(snap, asana.READY) + [
            t
            for t in asana.in_section(snap, asana.IN_PROGRESS)
            if str(t["gid"]) not in taken
        ]
        pick = [
            t
            for t in ready
            if str(t["gid"]) == str(payload["task_gid"]) and asana.allowed(t)
        ]
        if not pick:
            return {
                "outcome": "skipped",
                "reason": f"task {payload['task_gid']} is not ready for agent",
            }
        task = asana.developer_task(pick[0])
    elif task is None and asana.enabled():
        ready = asana.in_section(asana.load_snapshot(session)["tasks"], asana.READY)
        mine = [t for t in ready if asana.allowed(t)]
        task = next_asana_task(mine, taken, attempted_tasks(session))
        if task is None and ready and not mine:
            # Someone not on the allowlist moved every candidate: name them, build nothing.
            return {
                "outcome": "skipped",
                "reason": f"mover_not_allowed:{ready[0].get('moved_by') or 'unknown'}",
            }
    elif task is None:
        ready = issues.open_issues(REPO, token, issues.READY)
        mine = [i for i in ready if config.author_allowed(i["author"])]
        task = next_task(mine, taken, attempted_tasks(session))
        if task is None and ready and not mine:
            # Every candidate was someone else's issue: say so instead of "none labelled ready".
            return {
                "outcome": "skipped",
                "reason": f"author_not_allowed:{ready[0]['author']}",
            }
    elif str(task["gid"]) in taken:
        duplicate = next(
            (
                p["number"]
                for p in open_prs
                if p["branch"] == f"factory/task/{task['gid']}"
            ),
            None,
        )
    # Nothing new to build: a maintainer's comment on a task in review is the next job.
    if (
        task is None
        and asana.enabled()
        and not payload.get("task")
        and (rev := handle_revision(session, token, open_prs, payload)) is not None
    ):
        return rev
    if task is None:
        where = (
            f"Asana task in {asana.READY!r}"
            if asana.enabled()
            else f"open issue labelled {issues.READY}"
        )
        return {"outcome": "skipped", "reason": f"no new {where}"}
    publish = bool(DEV_PUBLISH and token and not payload.get("dry_run"))
    tracker = (
        # The run part of an op's idempotency key must differ per attempt, or a second attempt
        # on the same task would have its moves deduplicated against the first one's.
        asana.Board(
            session, "developer", f"task-{task['gid']}-{int(time.time())}", publish
        )
        if asana.enabled()
        else issues.Tracker(REPO, token, publish)
    )
    work = Path("/tmp/factory") / f"task-{task['gid']}"
    agent = Developer(task, clone(work), work, REGION, REPO, tracker)
    if str(task["gid"]) in taken:
        rec = {
            "outcome": "skipped_duplicate",
            "reason": f"issue already has an open factory PR (#{duplicate}) or a factory/task branch",
            "duplicate_of": duplicate,
        }
    elif (
        publish
        and sum(p["branch"].startswith("factory/task/") for p in open_prs)
        >= MAX_OPEN_FACTORY_PRS
    ):
        rec = {
            "outcome": "skipped",
            "reason": f"{MAX_OPEN_FACTORY_PRS} agent PRs already open",
        }
    else:
        rec = _safe(agent.execute)
        if publish and rec.get("outcome") == "branch_ready_dry_run":
            rec.update(_safe(lambda: agent.ship(token)))
        if rec.get("outcome") == "failed" and isinstance(tracker, asana.Board):
            # A crashed attempt must not leave the task stuck in progress: park it, say why.
            tracker.move(
                task["gid"],
                asana.NEEDS_SCOPING,
                f"Developer agent failed: {str(rec.get('reason', ''))[:300]}. "
                "Move the task back to Ready for agent to retry.",
            )
        elif (
            rec.get("outcome") == "branch_ready_dry_run"
        ):  # what a real run would do after opening the PR
            agent.move(
                "In review",
                f"Developer agent (dry run): would open PR '{rec['pr_title']}'",
            )
        rec["issue_ops"] = tracker.ops
    keys = (
        "plan",
        "checks",
        "pr_title",
        "pr_body",
        "pr_url",
        "files_changed",
        "refused",
        "issue_ops",
    )
    return _finish(agent, rec, not publish, keys)


def handle_design(payload: dict) -> dict:
    """Asana only: the next task in Needs design that an allowlisted person put there gets a spec."""
    if not asana.enabled():
        return {
            "outcome": "skipped",
            "reason": "designer runs only with FACTORY_TRACKER=asana",
        }
    session = boto3.Session(region_name=REGION)
    todo = asana.in_section(asana.load_snapshot(session)["tasks"], asana.NEEDS_DESIGN)
    mine = [t for t in todo if asana.allowed(t)]
    if payload.get("task_gid"):  # the planner names the task; it must still qualify
        mine = [t for t in mine if str(t["gid"]) == str(payload["task_gid"])]
        task = mine[0] if mine else None
    else:
        task = next_design(mine, attempted_designs(session))
    if task is None:
        reason = (
            f"mover_not_allowed:{todo[0].get('moved_by') or 'unknown'}"
            if todo and not mine
            else f"nothing new in {asana.NEEDS_DESIGN!r}"
        )
        return {"outcome": "skipped", "reason": reason}
    board = asana.Board(
        session, "designer", f"design-{task['gid']}-{int(time.time())}", PM_PUBLISH
    )
    work = Path("/tmp/factory") / f"design-{task['gid']}"
    agent = Designer(task, clone(work), work, REGION, REPO, board)
    return _finish(
        agent,
        _safe(agent.execute),
        not PM_PUBLISH,
        ("spec", "question", "issue_ops", "task_gid"),
    )


def board_link(
    pr: dict, token: str, publish: bool, agent: str, section: str, text: str
) -> dict:
    """Asana mode: put a PR's news on the task it names, and move the task to `section`.

    Only for a PR someone with write access stands behind: anyone can open a PR on a public
    repo and write any gid in it, and the board is private. A branch in this repository (not a
    fork) was pushed by someone with write access or by the factory's own App, so it counts;
    a fork's PR counts only if its author has write access. Only for a task on the board, open,
    and in one of the factory's sections; a task elsewhere on the board is left alone.
    """
    gid = asana.task_ref(pr)
    if not gid:
        return {}
    author = (pr.get("user") or {}).get("login", "")
    head_repo = (((pr.get("head") or {}).get("repo")) or {}).get("full_name", "")
    if not token or (head_repo != REPO and not _can_write(author, token)):
        return {"board": "skipped", "board_reason": f"@{author} has no write access"}
    session = boto3.Session(region_name=REGION)
    try:
        task = next(
            (t for t in asana.load_snapshot(session)["tasks"] if str(t["gid"]) == gid),
            None,
        )
    except asana.StaleSnapshot as e:
        return {"board": "skipped", "board_reason": str(e)}
    if (
        task is None
        or task.get("completed")
        or task.get("section") not in asana.SECTIONS
    ):
        return {
            "board": "skipped",
            "board_reason": f"task {gid} is not open in a factory section",
        }
    board = asana.Board(session, agent, f"pr-{pr['number']}", publish)
    if task.get("section") != section:
        board.move(gid, section)
    board.comment(gid, text)
    return {
        "board": "queued" if publish else "dry_run",
        "task_gid": gid,
        "issue_ops": board.ops,
    }


def handle_merge(payload: dict) -> dict:
    token = _github_token()
    pr = github.get_pull(REPO, int(payload["pull_request"]["number"]), token)
    if (
        pr.get("merged_at")
        and asana.enabled()
        and not pr["head"]["ref"].startswith("factory/")
    ):
        # A teammate's PR: no factory run rows to mark, but its task is done.
        work = Path("/tmp/factory") / f"merge-{pr['number']}"
        work.mkdir(parents=True, exist_ok=True)
        agent = Run(
            {"id": f"merge-{pr['number']}", "repository": {"full_name": REPO}},
            work,
            work,
            None,
            REGION,
        )
        agent.AGENT, agent.branch = "merge_handler", pr["head"]["ref"]
        link = board_link(
            pr, token, True, "merge_handler", asana.DONE, f"Merged: {pr['html_url']}"
        )
        rec = {
            "outcome": "merged_recorded" if link else "skipped",
            "reason": "" if link else "names no Asana task",
            **link,
        }
        return _finish(
            agent, rec, False, ("board", "board_reason", "task_gid", "issue_ops")
        )
    if not pr.get("merged_at") or not pr["head"]["ref"].startswith("factory/"):
        return {"outcome": "skipped", "reason": "not a merged factory PR"}
    agent = MergeHandler(
        pr, Path("/tmp/factory") / f"merge-{pr['number']}", REGION, REPO
    )
    agent.work.mkdir(parents=True, exist_ok=True)
    rec = _safe(agent.execute)
    if asana.enabled() and rec.get("outcome") == "merged_recorded":
        rec.update(
            board_link(
                pr,
                token,
                True,
                "merge_handler",
                asana.DONE,
                f"Merged: {pr['html_url']}",
            )
        )
    out = _finish(
        agent, rec, True, ("pr_url", "updated_runs", "board", "task_gid", "issue_ops")
    )
    if not asana.enabled():  # a merge changes the repo: refresh the issues
        out["product_manager"] = handle_pm({})
    # On the Asana board the daily PM run is the refresh: a run per merge meant parallel runs
    # proposing the same tasks when several PRs merged together.
    return out


WRITE_ROLES = {
    "admin",
    "maintain",
    "write",
}  # the commands in issue comments need write access


def _can_write(login: str, token: str) -> bool:
    try:
        perm = github.request(
            "GET", f"/repos/{REPO}/collaborators/{login}/permission", token
        )
    except RuntimeError as e:
        if "-> 404" in str(e):  # not a collaborator at all
            return False
        raise
    return perm.get("permission") in WRITE_ROLES


def _pm_comment(number: int, text: str, token: str) -> dict:
    """/pm: a consult answer, posted as a comment on the issue it was asked on."""
    issue = github.request("GET", f"/repos/{REPO}/issues/{number}", token)
    author = (issue.get("user") or {}).get("login", "")
    if not config.author_allowed(author):  # the PM comments only on allowlisted issues
        return {"outcome": "skipped", "reason": f"author_not_allowed:{author}"}
    work = Path("/tmp/factory") / "pm"
    agent = Consult(clone(work), work, REGION, REPO, token, str(number), text, True)
    rec = _safe(agent.execute)
    if rec.get("outcome") == "consult_answered":
        # The answer cost tokens, so this agent must record its own row: letting the comment raise
        # past _finish lost the row entirely, and the net in handle() would bill a fresh agent $0.
        posted = _safe(
            lambda: (
                agent.tracker.comment(number, "Factory PM:\n\n" + rec["answer"])
                or {"outcome": "comment_posted"}
            )
        )
        if posted.get("outcome") == "failed":
            rec["outcome"] = "consult_not_posted"
            rec["reason"] = f"answer written, not posted: {posted['reason']}"[:500]
    return _finish(
        agent,
        rec,
        False,
        ("answer", "conversation_id", "turn", "queued", "issue_ops", "dropped"),
    )


def _take_issue(number: int, token: str) -> dict:
    """/dev, or the factory:ready label: the developer takes this issue now (publishing per FACTORY_DEV_PUBLISH)."""
    issue = github.request("GET", f"/repos/{REPO}/issues/{number}", token)
    if "pull_request" in issue or issue["state"] != "open":
        return {"outcome": "skipped", "reason": "not an open issue"}
    author = (issue.get("user") or {}).get("login", "")
    # A maintainer's /dev or factory:ready label does not widen the allowlist: the issue's own
    # author decides. Nothing is posted, so the issue is left exactly as it was.
    if not config.author_allowed(author):
        return {"outcome": "skipped", "reason": f"author_not_allowed:{author}"}
    task = {
        "gid": str(number),
        "title": issue["title"],
        "body": issue.get("body") or "",
        "labels": [label["name"] for label in issue.get("labels", [])],
    }
    return handle_developer({"task": task})


def _decide(number: int, name: str, args: str, actor: str, token: str) -> dict:
    """/approve or /reject P-xxxxxx (or 'all'): only on the Factory digest issue, where proposals are posted."""
    digest, _ = issues.digest_issue(REPO, token, create=False)
    if digest is None or number != digest:
        return {
            "outcome": "skipped",
            "reason": "reply on the open 'Factory digest' issue",
        }
    pending = issues.pending_proposals(REPO, token, digest)
    verdict = "approved" if name == "approve" else "rejected"
    tracker = issues.Tracker(REPO, token, True)
    decisions = []
    for pid in list(pending) if args == "all" else args.split():
        if pid not in pending:
            decisions.append({"id": pid, "outcome": "not pending"})
            continue
        p = pending[pid]
        created = issues.apply_proposal(tracker, p) if verdict == "approved" else None
        note = f"{pid} {verdict} by @{actor}" + (f" as #{created}" if created else "")
        tracker.comment(
            digest, f"<!-- factory:resolved {pid} {verdict} -->\nFactory PM: {note}"
        )
        decisions.append({"id": pid, "outcome": verdict, "issue": created})
    return {"outcome": "decided", "decisions": decisions, "issue_ops": tracker.ops}


def handle_issue_command(payload: dict) -> dict:
    if asana.enabled():
        # The board is the control surface: a comment on a public issue starts nothing.
        return {"outcome": "skipped", "reason": "tracker is asana: use the Asana board"}
    token = _github_token()
    actor, number = payload["actor"]["login"], int(payload["issue"]["number"])
    if not _can_write(actor, token):
        return {"outcome": "skipped", "reason": f"@{actor} has no write access"}
    name, args = payload["command"], payload.get("args", "")
    if name == "pm":
        return _pm_comment(number, args, token)
    if name in ("dev", "ready"):
        return _take_issue(number, token)
    return _decide(number, name, args, actor, token)


def _post_digest_asana(summary: dict) -> dict:
    """The digest as a comment on the board's 'Factory digest' task (the sync creates it on first run)."""
    session = boto3.Session(region_name=REGION)
    tasks = asana.load_snapshot(session)["tasks"]
    task = next(
        (
            t
            for t in tasks
            if t["name"] == asana.DIGEST_TASK and asana.MARKER in (t.get("notes") or "")
        ),
        None,
    )
    token = _github_token()
    prs = [p["number"] for p in github.open_factory_prs(REPO, token)]
    text = render_digest(summary, [], prs)
    board = asana.Board(session, "digest", f"digest-{summary.get('day', '')}", True)
    if task is None:
        board.create(
            asana.BACKLOG,
            asana.DIGEST_TASK,
            "Daily factory digests, one comment per day.\n\n" + text,
        )
        return {"task": "created", "issue_ops": board.ops}
    board.comment(task["gid"], text)
    return {"task": task["gid"], "issue_ops": board.ops}


def _post_digest(summary: dict) -> dict:
    """The daily digest as a comment on the pinned 'Factory digest' issue (created and pinned on first run)."""
    token = _github_token()
    number, _ = issues.digest_issue(REPO, token, create=True)
    pinned = issues.pin(
        REPO, token, number
    )  # every run: an existing issue gets pinned once the App can
    pending = sorted(issues.pending_proposals(REPO, token, number))
    prs = [p["number"] for p in github.open_factory_prs(REPO, token)]
    issues.Tracker(REPO, token, True).comment(
        number, render_digest(summary, pending, prs)
    )
    return {"issue": number, "pin": pinned}


def _trigger(payload: dict) -> str:
    """What an unrecorded run row points at: the id validate() gives the real run, or the issue."""
    try:
        return str(validate(payload).get("id", "") or payload.get("role", ""))
    except (ValueError, KeyError, TypeError):
        number = (payload.get("issue") or {}).get("number")  # the issue-command roles
        return str(number if number else payload.get("role", ""))


def handle(payload: dict) -> dict:
    role = payload.get("role")
    # One invocation is one run, whatever agents it builds.
    ci_fixer.SWALLOWED.clear()
    ci_fixer.REFUSED.clear()
    # Pre-flight: a run that cannot finish is skipped here, before clone() and any model call.
    # The bench reviews a local patch and never writes to GitHub, so it needs no permission.
    if "bench" not in payload and (skip := preflight(role)):
        return _record_row(role, _trigger(payload), skip)
    out = _safe(lambda: _dispatch(role, payload))
    # Every invocation leaves exactly one row. A handler that returned early (no task, no write
    # access, a refused author) or raised before its own _finish (a clone, a token mint, a comment
    # that 403s) recorded nothing, and an unrecorded run can be neither noticed nor counted.
    return out if "run_id" in out else _record_row(role, _trigger(payload), out)


def _dispatch(role: str, payload: dict) -> dict:
    if role == "issue_command":
        return handle_issue_command(
            payload
        )  # handle() wraps this; no second _safe needed
    if role == "pr_reviewer":
        return handle_review(payload)
    if role == "product_manager":
        return handle_pm(payload)
    if role == "developer":
        return handle_developer(payload)
    if role == "designer":
        return handle_design(payload)
    if role == "merge_handler":
        return handle_merge(payload)
    if role == "digest":
        work = Path("/tmp/factory") / "digest"
        work.mkdir(parents=True, exist_ok=True)
        agent = Digest(work, REGION, REPO, payload.get("day"))
        rec = _safe(agent.execute)
        if rec.get("outcome") == "digest_written":
            poster = _post_digest_asana if asana.enabled() else _post_digest
            rec["digest_post"] = post = _safe(lambda: poster(rec["summary"]))
            if post.get("outcome") == "failed":
                # The summary was computed and stored, but nobody can read it on the issue.
                # An outcome that claims success for a partial result is the bug being fixed.
                rec["outcome"] = "digest_not_posted"
                rec["reason"] = f"digest written, not posted: {post['reason']}"[:500]
        return _finish(agent, rec, False, ("summary", "digest_post"))
    run = validate(payload)
    work = Path("/tmp/factory") / str(run["id"])
    repo = clone(work)
    agent = Run(run, repo, work, None, REGION)
    creds = None
    try:
        creds = load_app_creds()
        token, missing = github.installation_token(creds, REPO) if creds else ("", [])
        log_text = payload.get("log_text") or ""
        if not log_text and token:
            log_text = github.failed_job_logs(REPO, run["id"], token)
        agent.open_prs = github.open_factory_prs(
            REPO, token
        )  # dedupe guard (public repo: works anonymously)
        # Without a token the agent re-runs the workflow's check to get the failure text.
        rec = agent.execute(log_text)
        if missing:
            rec["github_missing_permissions"] = ", ".join(missing)
        if token and rec["outcome"] == "fix_ready_dry_run":
            if PUBLISH:
                rec.update(agent.publish(token))
            else:
                rec["reason"] = (
                    "publish disabled (FACTORY_PUBLISH is not true): branch and PR not created"
                )
    except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
        traceback.print_exc()
        rec = {
            "outcome": "failed",
            "reason": f"{type(e).__name__}: {e}"[:500],
            "check_passed": False,
        }
    line = agent.record(rec, dry_run=not (creds and PUBLISH))
    print(json.dumps({"factory_run": line}), flush=True)
    return {
        **line,
        **{
            k: rec[k]
            for k in ("pr_title", "pr_body", "diff", "triage", "pr_url")
            if rec.get(k)
        },
    }


def _run_locked(payload: dict) -> dict:
    try:
        return handle(payload)
    finally:
        _state["last_update"] = int(time.time())
        _busy.release()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/ping":
            return self._send(404, {"error": "not found"})
        status = "HealthyBusy" if _busy.locked() else "Healthy"
        return self._send(
            200, {"status": status, "time_of_last_update": _state["last_update"]}
        )

    def do_POST(self):
        if self.path != "/invocations":
            return self._send(404, {"error": "not found"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY:
            return self._send(413, {"error": "payload too large"})
        try:
            payload = json.loads(self.rfile.read(size) or b"{}")
            validate(
                dict(payload, workflow_run=dict(payload.get("workflow_run") or {}))
            )
        except (ValueError, KeyError, TypeError) as e:
            return self._send(400, {"error": str(e)})
        if not _busy.acquire(blocking=False):
            return self._send(200, {"status": "already running in this session"})
        if payload.get("role") == "verify_app":
            try:
                return self._send(200, verify_app())
            finally:
                _busy.release()
        if payload.get("wait"):
            return self._send(200, _run_locked(payload))
        threading.Thread(target=_run_locked, args=(payload,), daemon=True).start()
        return self._send(
            200,
            {
                "accepted": validate(
                    dict(payload, workflow_run=dict(payload.get("workflow_run") or {}))
                )["id"]
            },
        )


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # 0.0.0.0 inside the container only: AgentCore routes to it, no port is published.
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
