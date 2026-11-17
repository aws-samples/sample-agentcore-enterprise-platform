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

from factory import github
from factory.ci_fixer import Run
from factory.reviewer import Review

REPO = os.environ.get(
    "FACTORY_REPO", "aws-samples/sample-agentcore-enterprise-platform"
)
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_BODY = 5 * 1024 * 1024
# Pushing branches and opening PRs needs an explicit switch on top of the App secret.
PUBLISH = os.environ.get("FACTORY_PUBLISH", "false") == "true"
# Reviews are posted only with their own switch, separate from CI-fix PRs.
REVIEW_PUBLISH = os.environ.get("FACTORY_REVIEW_PUBLISH", "false") == "true"
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
        return {"id": number}
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


def handle(payload: dict) -> dict:
    if payload.get("role") == "pr_reviewer":
        return handle_review(payload)
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
