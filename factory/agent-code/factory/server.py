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

from factory import github, issues
from factory.ci_fixer import Run
from factory.consult import Consult
from factory.developer import (
    Developer,
    MergeHandler,
    attempted_tasks,
    next_task,
    taken_tasks,
)
from factory.digest import Digest
from factory.digest import render as render_digest
from factory.pm import ProductManager
from factory.reviewer import Review, build_review

REPO = os.environ.get(
    "FACTORY_REPO", "aws-samples/sample-agentcore-enterprise-platform"
)
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_BODY = 5 * 1024 * 1024
# Pushing branches and opening PRs needs an explicit switch on top of the App secret.
PUBLISH = os.environ.get("FACTORY_PUBLISH", "false") == "true"
# Reviews are posted only with their own switch, separate from CI-fix PRs.
REVIEW_PUBLISH = os.environ.get("FACTORY_REVIEW_PUBLISH", "false") == "true"
# Developer PRs need their own switch; otherwise the branch stays local.
DEV_PUBLISH = os.environ.get("FACTORY_DEV_PUBLISH", "false") == "true"
# PM (and consult) issue writes need their own switch; otherwise the run records what it would do.
PM_PUBLISH = os.environ.get("FACTORY_PM_PUBLISH", "false") == "true"
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
    if payload.get("role") == "developer":
        task = payload.get("task")
        if task is not None and not (
            re.fullmatch(r"\d{1,20}", str(task.get("gid", "")))
            and str(task.get("title", "")).strip()
        ):
            raise ValueError("task needs a numeric gid and a title")
        return {"id": f"task-{(task or {}).get('gid', 'next')}"}
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
                rec.update(
                    outcome="review_posted",
                    pr_url=github.post_review(REPO, number, token, rec["review"]),
                )
        except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
            traceback.print_exc()
            rec = {"outcome": "failed", "reason": f"{type(e).__name__}: {e}"[:500]}
    line = agent.record(rec, dry_run=not (token and REVIEW_PUBLISH))
    print(json.dumps({"factory_run": line}), flush=True)
    return {
        **line,
        **{
            k: rec[k]
            for k in ("review", "verdict", "summary", "model_reason", "files_skipped")
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
    review = build_review(result, files, head, "+".join(item.get("models") or {}), [])
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
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
        traceback.print_exc()
        return {"outcome": "failed", "reason": f"{type(e).__name__}: {e}"[:500]}


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


def handle_developer(payload: dict) -> dict:
    token = _github_token()
    session = boto3.Session(region_name=REGION)
    task = payload.get("task")
    open_prs = github.open_factory_prs(REPO, token)
    taken = taken_tasks(open_prs, github.factory_task_branches(REPO, token))
    duplicate = None
    if task is None:
        task = next_task(
            issues.open_issues(REPO, token, issues.READY),
            taken,
            attempted_tasks(session),
        )
    elif str(task["gid"]) in taken:
        duplicate = next(
            (
                p["number"]
                for p in open_prs
                if p["branch"] == f"factory/task/{task['gid']}"
            ),
            None,
        )
    if task is None:
        return {
            "outcome": "skipped",
            "reason": f"no new open issue labelled {issues.READY}",
        }
    publish = bool(DEV_PUBLISH and token and not payload.get("dry_run"))
    tracker = issues.Tracker(REPO, token, publish)
    work = Path("/tmp/factory") / f"task-{task['gid']}"
    agent = Developer(task, clone(work), work, REGION, REPO, tracker)
    if str(task["gid"]) in taken:
        rec = {
            "outcome": "skipped_duplicate",
            "reason": f"issue already has an open factory PR (#{duplicate}) or a factory/task branch",
            "duplicate_of": duplicate,
        }
    elif (
        publish and sum(p["branch"].startswith("factory/task/") for p in open_prs) >= 2
    ):
        rec = {"outcome": "skipped", "reason": "2 agent PRs already open"}
    else:
        rec = _safe(agent.execute)
        if publish and rec.get("outcome") == "branch_ready_dry_run":
            rec.update(_safe(lambda: agent.ship(token)))
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


def handle_merge(payload: dict) -> dict:
    token = _github_token()
    pr = github.get_pull(REPO, int(payload["pull_request"]["number"]), token)
    if not pr.get("merged_at") or not pr["head"]["ref"].startswith("factory/"):
        return {"outcome": "skipped", "reason": "not a merged factory PR"}
    agent = MergeHandler(
        pr, Path("/tmp/factory") / f"merge-{pr['number']}", REGION, REPO
    )
    agent.work.mkdir(parents=True, exist_ok=True)
    out = _finish(agent, _safe(agent.execute), True, ("pr_url", "updated_runs"))
    out["product_manager"] = handle_pm(
        {}
    )  # a merge changes the repo: refresh the issues
    return out


WRITE_ROLES = {"admin", "maintain", "write"}  # the commands in issue comments need write access


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
    work = Path("/tmp/factory") / "pm"
    agent = Consult(clone(work), work, REGION, REPO, token, str(number), text, True)
    rec = _safe(agent.execute)
    if rec.get("outcome") == "consult_answered":
        agent.tracker.comment(number, "Factory PM:\n\n" + rec["answer"])
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
        return {"outcome": "skipped", "reason": "reply on the open 'Factory digest' issue"}
    pending = issues.pending_proposals(REPO, token, digest)
    verdict = "approved" if name == "approve" else "rejected"
    tracker = issues.Tracker(REPO, token, True)
    decisions = []
    for pid in list(pending) if args == "all" else args.split():
        if pid not in pending:
            decisions.append({"id": pid, "outcome": "not pending"})
            continue
        p = pending[pid]
        created = (
            tracker.create(p["title"], p["body"], p["labels"])
            if verdict == "approved"
            else None
        )
        note = f"{pid} {verdict} by @{actor}" + (f" as #{created}" if created else "")
        tracker.comment(
            digest, f"<!-- factory:resolved {pid} {verdict} -->\nFactory PM: {note}"
        )
        decisions.append({"id": pid, "outcome": verdict, "issue": created})
    return {"outcome": "decided", "decisions": decisions, "issue_ops": tracker.ops}


def handle_issue_command(payload: dict) -> dict:
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


def _post_digest(summary: dict) -> dict:
    """The daily digest as a comment on the pinned 'Factory digest' issue (created and pinned on first run)."""
    token = _github_token()
    number, created = issues.digest_issue(REPO, token, create=True)
    pinned = issues.pin(REPO, token, number) if created else "existing issue"
    pending = sorted(issues.pending_proposals(REPO, token, number))
    prs = [p["number"] for p in github.open_factory_prs(REPO, token)]
    issues.Tracker(REPO, token, True).comment(
        number, render_digest(summary, pending, prs)
    )
    return {"issue": number, "pin": pinned}


def handle(payload: dict) -> dict:
    role = payload.get("role")
    if role == "issue_command":
        return _safe(lambda: handle_issue_command(payload))
    if role == "pr_reviewer":
        return handle_review(payload)
    if role == "product_manager":
        return handle_pm(payload)
    if role == "developer":
        return handle_developer(payload)
    if role == "merge_handler":
        return handle_merge(payload)
    if role == "digest":
        work = Path("/tmp/factory") / "digest"
        work.mkdir(parents=True, exist_ok=True)
        agent = Digest(work, REGION, REPO, payload.get("day"))
        rec = _safe(agent.execute)
        if rec.get("outcome") == "digest_written":
            rec["digest_post"] = _safe(lambda: _post_digest(rec["summary"]))
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
