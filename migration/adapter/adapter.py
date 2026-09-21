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
    MIGRATION_ENV_JSON       extra child env as a JSON object of string values
                             (preferred; commas and equals are lossless)
    MIGRATION_ENV            legacy extra child env, "KEY=VALUE,KEY2=VALUE2"
    MIGRATION_SECRETS        comma-separated ENV NAMES resolved from Secrets
                             Manager into the CHILD's env only (never ours):
                             secret id <PROJECT_NAME>/<ENVIRONMENT>/migration/<NAME>
    MIGRATION_SECRET_ARNS    optional per-name override, "NAME=arn,NAME2=arn2"
    ADAPTER_CHILD_CMD        JSON array — the source image's Entrypoint+Cmd,
                             baked at image build by the migration buildspec
    ADAPTER_CHILD_USER       source image's configured runtime user, baked from
                             the CHILD_USER build argument; the adapter and
                             child both run as this user
    MIGRATION_STARTUP_TIMEOUT  seconds to wait for child health (default 60)
    MIGRATION_LIVE_HEALTH_TIMEOUT  per-invocation child health check timeout
                                  (default 2s, capped at 5s)
    PROJECT_NAME / ENVIRONMENT  used for the default secret naming above

GET /ping is BedrockAgentCoreApp's default handler: it reflects ADAPTER
liveness, not the child's. Each live invocation therefore checks both the child
process and its configured health endpoint before forwarding. The supervisor
thread restarts a dead child once; after a second death every invocation
returns a structured error instead.
"""

import json
import logging
import math
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
LIVE_HEALTH_TIMEOUT_DEFAULT = 2.0
LIVE_HEALTH_TIMEOUT_MAX = 5.0


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


def parse_env_json(raw: str) -> dict[str, str]:
    """Parse the lossless child-env contract without echoing its values."""
    message = "MIGRATION_ENV_JSON must be a valid JSON object of string values"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(message) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise ValueError(message)
    return parsed


def migration_env(environ: dict) -> dict[str, str]:
    """Prefer lossless JSON; retain comma pairs only for legacy callers."""
    if "MIGRATION_ENV_JSON" in environ:
        return parse_env_json(environ["MIGRATION_ENV_JSON"])
    return parse_pairs(environ.get("MIGRATION_ENV", ""))


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
    """Child env = our env + declared migration env + resolved secrets.
    Secrets land in the CHILD's env only; the adapter's own environ is untouched."""
    child = dict(environ)
    child.update(migration_env(environ))
    child.update(secrets)
    return child


def validate_runtime_user(environ: dict, effective_uid: int | None = None) -> None:
    """Fail closed if a declared non-root source user was lost during wrapping.

    Docker applies CHILD_USER to the adapter process. The child inherits that
    same uid from subprocess.Popen, so checking the adapter uid protects both.
    """
    expected = environ.get("ADAPTER_CHILD_USER", "").strip()
    if not expected:
        raise RuntimeError(
            "ADAPTER_CHILD_USER is missing; rebuild the adapter with "
            "--build-arg CHILD_USER=<source image Config.User>"
        )

    principal = expected.partition(":")[0]
    uid = os.geteuid() if effective_uid is None else effective_uid
    expected_is_root = principal in {"0", "root"}
    if not expected_is_root and uid == 0:
        raise RuntimeError(
            f"source image declared non-root user {expected!r}, but the adapter "
            "is running as root"
        )
    if principal.isdigit() and int(principal) != uid:
        raise RuntimeError(
            f"adapter uid {uid} does not match declared source image user {expected!r}"
        )


def live_health_timeout(environ: dict) -> float:
    """Return a positive per-invocation timeout capped at five seconds."""
    raw = environ.get("MIGRATION_LIVE_HEALTH_TIMEOUT", str(LIVE_HEALTH_TIMEOUT_DEFAULT))
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = LIVE_HEALTH_TIMEOUT_DEFAULT
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = LIVE_HEALTH_TIMEOUT_DEFAULT
    return min(timeout, LIVE_HEALTH_TIMEOUT_MAX)


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
            remaining = max(deadline - time.monotonic(), 0.05)
            with urllib.request.urlopen(  # nosec B310 — localhost http
                url, timeout=min(2, remaining)
            ) as resp:
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


def check_child_health(
    url: str, timeout: float, require_2xx: bool = True
) -> tuple[bool, str]:
    """Perform one bounded health probe for the live invocation path."""
    try:
        with urllib.request.urlopen(  # nosec B310 — localhost http
            url, timeout=min(max(timeout, 0.05), LIVE_HEALTH_TIMEOUT_MAX)
        ) as resp:
            if 200 <= resp.status < 400:
                return True, f"HTTP {resp.status}"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        exc.close()
        if not require_2xx:
            return True, f"HTTP {exc.code}"
        return False, f"HTTP {exc.code}"
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


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
    if _child is None or not _child.alive():
        return {
            "status": "error",
            "code": 503,
            "error": "migrated agent process is not running; see the runtime "
            "logs for its stderr",
        }

    declared_health = bool(os.environ.get("MIGRATION_HEALTH_PATH", ""))
    healthy, detail = check_child_health(
        _health_url(),
        live_health_timeout(dict(os.environ)),
        require_2xx=declared_health,
    )
    if not healthy:
        log.warning("rejecting invocation: child health check failed (%s)", detail)
        return {
            "status": "error",
            "code": 503,
            "error": f"migrated agent health check failed ({detail})",
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
        validate_runtime_user(dict(os.environ))
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(5)

    try:
        secrets = resolve_secrets(dict(os.environ))
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(3)

    try:
        child_env = build_child_env(dict(os.environ), secrets)
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(6)

    global _child
    _child = Child(cmd, child_env)
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
