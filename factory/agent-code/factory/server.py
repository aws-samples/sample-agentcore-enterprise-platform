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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3

from factory.ci_fixer import Run

REPO = os.environ.get(
    "FACTORY_REPO", "aws-samples/sample-agentcore-enterprise-platform"
)
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_BODY = 5 * 1024 * 1024
_busy = threading.Lock()  # one run per session; each session is its own microVM
_state = {"last_update": int(time.time())}


def github_app_configured() -> bool:
    """The App secret is an empty placeholder until the user creates the GitHub App."""
    name = os.environ.get("FACTORY_GITHUB_APP_SECRET")
    if not name:
        return False
    raw = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
        SecretId=name
    )["SecretString"]
    creds = json.loads(raw or "{}")
    return bool(
        creds.get("app_id")
        and creds.get("installation_id")
        and creds.get("private_key_pem")
    )


def fetch_failed_log(run_id: int) -> str:
    # shortcut: anonymous GitHub API (60 req/h, may need auth for logs); switch to the App installation token in step 3.
    def get(url: str):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "software-factory",
            },
        )
        return urllib.request.urlopen(req, timeout=60)

    base = f"https://api.github.com/repos/{REPO}/actions"
    with get(f"{base}/runs/{run_id}/jobs?filter=latest") as r:
        jobs = json.load(r)["jobs"]
    logs = []
    for job in jobs:
        if job.get("conclusion") == "failure":
            with get(f"{base}/jobs/{int(job['id'])}/logs") as r:
                logs.append(r.read().decode(errors="replace")[-2_000_000:])
    return "\n".join(logs)


def validate(payload: dict) -> dict:
    """Trust boundary: the payload comes from the dispatcher or a human invoke."""
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


def handle(payload: dict) -> dict:
    run = validate(payload)
    work = Path("/tmp/factory") / str(run["id"])
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
    agent = Run(run, repo, work, None, REGION)
    try:
        log_text = payload.get("log_text") or ""
        if not log_text:
            try:
                log_text = fetch_failed_log(run["id"])
            except (
                urllib.error.URLError
            ) as e:  # job logs need auth; the agent reproduces the check instead
                print(f"log fetch failed ({e}); reproducing the check locally")
        rec = agent.execute(log_text)
        if github_app_configured():
            rec["reason"] = (
                rec.get("reason", "")
                + " github app configured; push path not built yet (step 3)"
            ).strip()
    except Exception as e:  # noqa: BLE001 - every failure must still produce a run row
        traceback.print_exc()
        rec = {
            "outcome": "failed",
            "reason": f"{type(e).__name__}: {e}"[:500],
            "check_passed": False,
        }
    line = agent.record(rec, dry_run=True)
    print(json.dumps({"factory_run": line}), flush=True)
    return {
        **line,
        **{k: rec[k] for k in ("pr_title", "pr_body", "diff", "triage") if rec.get(k)},
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
        if payload.get("wait"):
            return self._send(200, _run_locked(payload))
        threading.Thread(target=_run_locked, args=(payload,), daemon=True).start()
        return self._send(200, {"accepted": payload["workflow_run"]["id"]})


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # 0.0.0.0 inside the container only: AgentCore routes to it, no port is published.
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
