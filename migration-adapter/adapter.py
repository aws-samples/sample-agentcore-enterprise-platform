"""Migration adapter — exposes the AgentCore Runtime contract in front of an
unmodified customer container.

AgentCore Runtime is arm64-only and speaks a fixed HTTP contract (port 8080,
POST /invocations, GET /ping). A customer's existing agent container speaks its
own HTTP. This adapter is baked ON TOP of the customer's image (see the
Dockerfile template next to this file): it starts the original command as a
child process, waits for it to become healthy, and forwards each /invocations
payload to the child over localhost. The customer's code does not change.

Runtime environment contract (set by stacks/runtime_stack.py in migration mode,
names emitted by the platform.yaml `migration:` block — see README.md):

    MIGRATION_PORT           child's listen port           (default 8000)
    MIGRATION_INVOKE_PATH    child's invocation endpoint   (default /invocations)
    MIGRATION_HEALTH_PATH    child's health endpoint; empty means "any HTTP
                             response counts as up" (no assumption about the
                             customer exposing a health route)
    MIGRATION_ENV            extra child env, "KEY=VALUE,KEY2=VALUE2"
    MIGRATION_SECRETS        comma-separated ENV NAMES resolved from Secrets
                             Manager into the CHILD's env only (never ours):
                             secret id <PROJECT_NAME>/<ENVIRONMENT>/migration/<NAME>
    MIGRATION_SECRET_ARNS    optional per-name override, "NAME=arn,NAME2=arn2"
    ADAPTER_CHILD_CMD        JSON array — the source image's Entrypoint+Cmd,
                             baked at image build by the migration buildspec
    MIGRATION_STARTUP_TIMEOUT  seconds to wait for child health (default 60)
    PROJECT_NAME / ENVIRONMENT  used for the default secret naming above

GET /ping is BedrockAgentCoreApp's default handler: it reflects ADAPTER
liveness, not the child's (there is no universal health hook on an arbitrary
customer container). The supervisor thread restarts a dead child once; after a
second death every invocation returns a structured error instead.
"""

import json
import logging
import os
import subprocess  # nosec B404 — supervising the customer's own command is the point
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(level=logging.INFO, format="[adapter] %(levelname)s %(message)s")
log = logging.getLogger("migration-adapter")

app = BedrockAgentCoreApp()

STDERR_TAIL_LINES = 50


def parse_pairs(raw: str) -> dict[str, str]:
    """Parse "KEY=VALUE,KEY2=VALUE2" (values may contain '='; ',' may not)."""
    pairs = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"malformed KEY=VALUE entry: {item!r}")
        key, _, value = item.partition("=")
        pairs[key.strip()] = value
    return pairs


def secret_id_for(name: str, environ: dict) -> str:
    """Secrets Manager id for a declared secret: explicit ARN override, else
    the platform naming convention <project>/<environment>/migration/<NAME>."""
    overrides = parse_pairs(environ.get("MIGRATION_SECRET_ARNS", ""))
    if name in overrides:
        return overrides[name]
    project = environ.get("PROJECT_NAME", "")
    env = environ.get("ENVIRONMENT", "")
    return f"{project}/{env}/migration/{name}"


def resolve_secrets(environ: dict, client=None) -> dict[str, str]:
    """Fetch declared secrets. Fails fast naming the missing secret — a child
    started with a missing credential fails later and further from the cause."""
    names = [
        n.strip() for n in environ.get("MIGRATION_SECRETS", "").split(",") if n.strip()
    ]
    if not names:
        return {}
    if client is None:
        import boto3  # lazy: the no-secrets path (and local finch runs) never needs it

        client = boto3.client("secretsmanager")
    resolved = {}
    for name in names:
        secret_id = secret_id_for(name, environ)
        try:
            resolved[name] = client.get_secret_value(SecretId=secret_id)["SecretString"]
        except Exception as exc:
            raise RuntimeError(
                f"could not resolve declared migration secret {name!r} "
                f"(Secrets Manager id {secret_id!r}): {exc}"
            ) from exc
    return resolved


def build_child_env(environ: dict, secrets: dict[str, str]) -> dict[str, str]:
    """Child env = our env + MIGRATION_ENV pairs + resolved secrets.
    Secrets land in the CHILD's env only; the adapter's own environ is untouched."""
    child = dict(environ)
    child.update(parse_pairs(environ.get("MIGRATION_ENV", "")))
    child.update(secrets)
    return child


class Child:
    """The customer's process: start, tail stderr, restart once, report state."""

    def __init__(self, cmd: list[str], env: dict[str, str]):
        self.cmd = cmd
        self.env = env
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self.restarts_used = 0
        self.unhealthy = False
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self):
        log.info("starting child: %s", self.cmd)
        self.proc = subprocess.Popen(  # nosec B603 — cmd is the source image's own Entrypoint+Cmd
            self.cmd,
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
        )
        threading.Thread(
            target=self._pump_stderr, args=(self.proc,), daemon=True
        ).start()

    def _pump_stderr(self, proc):
        for line in proc.stderr:
            self.stderr_tail.append(line.rstrip("\n"))
            sys.stderr.write(f"[child] {line}")

    def supervise(self):
        """Blockingly watch the child; restart once, then mark unhealthy.
        Run in a daemon thread."""
        while True:
            self.proc.wait()
            with self._lock:
                code = self.proc.returncode
                if self.restarts_used >= 1:
                    self.unhealthy = True
                    log.error("child exited again (rc=%s); giving up", code)
                    return
                self.restarts_used += 1
            log.warning("child exited (rc=%s); restarting once", code)
            self.start()

    def alive(self) -> bool:
        return not self.unhealthy and self.proc is not None and self.proc.poll() is None


def wait_for_child(
    url: str,
    timeout: float,
    child: Child | None = None,
    interval: float = 0.5,
    require_2xx: bool = True,
) -> None:
    """Poll the child's health URL until 2xx/3xx, bounded.

    require_2xx=False is the "customer declared no health path" mode: any HTTP
    response (even a 404 on "/") proves the server is accepting requests.
    """
    deadline = time.monotonic() + timeout
    last_error = "no response yet"
    while time.monotonic() < deadline:
        if child is not None and child.unhealthy:
            break
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # nosec B310 — localhost http
                if 200 <= resp.status < 400:
                    return
                last_error = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if not require_2xx:
                return  # the server answered; that's all an undeclared path can prove
            last_error = f"HTTP {exc.code}"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(interval)
    tail = "\n".join(child.stderr_tail) if child else ""
    raise TimeoutError(
        f"child did not become healthy at {url} within {timeout:.0f}s "
        f"(last error: {last_error})\n--- child stderr tail ---\n{tail}"
    )


def forward(payload, url: str):
    """POST the invocation payload to the child and map its reply.

    JSON reply → returned as-is (parsed). Plain text → wrapped as a success.
    Non-2xx → structured error. text/event-stream → a generator of the events'
    data payloads; BedrockAgentCoreApp re-emits each yield as an SSE event.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload if payload is not None else {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req)  # nosec B310 — localhost http
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return {"status": "error", "code": exc.code, "error": body[:2000]}
    except OSError as exc:
        return {"status": "error", "code": 502, "error": f"child unreachable: {exc}"}

    content_type = resp.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        return _stream_events(resp)
    body = resp.read().decode(errors="replace")
    resp.close()
    if "application/json" in content_type:
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass  # advertised JSON but wasn't; fall through to the text wrap
    return {"status": "success", "response": body}


def _stream_events(resp):
    """Yield each SSE event's data payload (parsed when JSON) from the child.
    Sync generator: BedrockAgentCoreApp streams it back out as SSE."""
    try:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                yield data
    finally:
        resp.close()


# Module state wired by main(); the entrypoint reads env per-call so tests can
# exercise forward() without a running app.
_child: Child | None = None


def _invoke_url() -> str:
    port = os.environ.get("MIGRATION_PORT", "8000")
    path = os.environ.get("MIGRATION_INVOKE_PATH", "/invocations")
    return f"http://127.0.0.1:{port}{path}"


def _health_url() -> str:
    port = os.environ.get("MIGRATION_PORT", "8000")
    path = os.environ.get("MIGRATION_HEALTH_PATH", "") or "/"
    return f"http://127.0.0.1:{port}{path}"


@app.entrypoint
def invoke(payload=None):
    if _child is not None and not _child.alive():
        return {
            "status": "error",
            "code": 503,
            "error": "migrated agent process is not running (exited twice); "
            "see the runtime logs for its stderr",
        }
    return forward(payload, _invoke_url())


def main():
    cmd_raw = os.environ.get("ADAPTER_CHILD_CMD", "")
    try:
        cmd = json.loads(cmd_raw)
        assert isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd)
    except (json.JSONDecodeError, AssertionError):
        log.error(
            "ADAPTER_CHILD_CMD must be a non-empty JSON array of strings, got: %r",
            cmd_raw,
        )
        sys.exit(2)

    try:
        secrets = resolve_secrets(dict(os.environ))
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(3)

    global _child
    _child = Child(cmd, build_child_env(dict(os.environ), secrets))
    _child.start()
    threading.Thread(target=_child.supervise, daemon=True).start()

    timeout = float(os.environ.get("MIGRATION_STARTUP_TIMEOUT", "60"))
    declared_health = bool(os.environ.get("MIGRATION_HEALTH_PATH", ""))
    try:
        wait_for_child(_health_url(), timeout, _child, require_2xx=declared_health)
    except TimeoutError as exc:
        log.error("%s", exc)
        sys.exit(4)

    log.info("child healthy; serving the AgentCore contract on :8080")
    app.run()


if __name__ == "__main__":
    main()
