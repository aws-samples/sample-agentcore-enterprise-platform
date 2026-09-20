"""Migration adapter unit tests — migration-adapter/adapter.py.

Pure unit: the "customer agent" is a threaded stdlib http.server, Secrets
Manager is a fake client object, and BedrockAgentCoreApp is stubbed when the
package is absent (it lives in the containers, not this venv). No AWS, no
network beyond 127.0.0.1.
"""

import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# ── import adapter.py with bedrock_agentcore stubbed if unavailable ──
try:
    import bedrock_agentcore.runtime  # noqa: F401
except ImportError:

    class _FakeApp:
        def entrypoint(self, fn):
            return fn

        def run(self, *args, **kwargs):
            raise RuntimeError("app.run() must not be reached in unit tests")

    _fake_runtime = types.ModuleType("bedrock_agentcore.runtime")
    _fake_runtime.BedrockAgentCoreApp = _FakeApp
    _fake_pkg = types.ModuleType("bedrock_agentcore")
    _fake_pkg.runtime = _fake_runtime
    sys.modules["bedrock_agentcore"] = _fake_pkg
    sys.modules["bedrock_agentcore.runtime"] = _fake_runtime

_spec = importlib.util.spec_from_file_location(
    "migration_adapter", REPO / "migration-adapter" / "adapter.py"
)
adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter)


# ── stub child server ──
class _ChildHandler(BaseHTTPRequestHandler):
    """Behaviour is driven by the path so one server covers every case."""

    def _reply(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, b'{"status":"ok"}', "application/json")
        else:
            self._reply(404, b"nope", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/json":
            self._reply(
                200,
                json.dumps({"answer": payload.get("prompt", "")}).encode(),
                "application/json",
            )
        elif self.path == "/text":
            self._reply(200, b"plain pong", "text/plain")
        elif self.path == "/boom":
            self._reply(500, b"child exploded", "text/plain")
        elif self.path == "/sse":
            body = b'data: {"delta": "a"}\n\ndata: raw-text\n\nignored line\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._reply(404, b"nope", "text/plain")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def child_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChildHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ── forwarding / response mapping ──
def test_json_reply_passes_through(child_server):
    result = adapter.forward({"prompt": "hi"}, f"{child_server}/json")
    assert result == {"answer": "hi"}


def test_text_reply_is_wrapped(child_server):
    result = adapter.forward({"prompt": "hi"}, f"{child_server}/text")
    assert result == {"status": "success", "response": "plain pong"}


def test_non_2xx_maps_to_structured_error(child_server):
    result = adapter.forward({}, f"{child_server}/boom")
    assert result["status"] == "error"
    assert result["code"] == 500
    assert "child exploded" in result["error"]


def test_unreachable_child_maps_to_502():
    result = adapter.forward({}, "http://127.0.0.1:1/nothing")
    assert result["status"] == "error"
    assert result["code"] == 502


def test_sse_reply_streams_event_payloads(child_server):
    result = adapter.forward({}, f"{child_server}/sse")
    events = list(result)  # a generator, not a dict
    assert events == [{"delta": "a"}, "raw-text"]


# ── health wait ──
def test_wait_for_child_succeeds_on_2xx(child_server):
    adapter.wait_for_child(f"{child_server}/healthz", timeout=5)


def test_wait_for_child_times_out_with_diagnostics():
    child = adapter.Child(["true"], {})
    child.stderr_tail.extend(["boom line 1", "boom line 2"])
    with pytest.raises(TimeoutError) as exc:
        adapter.wait_for_child(
            "http://127.0.0.1:1/healthz", timeout=0.3, child=child, interval=0.1
        )
    assert "did not become healthy" in str(exc.value)
    assert "boom line 2" in str(exc.value)


def test_wait_for_child_declared_path_rejects_404(child_server):
    with pytest.raises(TimeoutError):
        adapter.wait_for_child(
            f"{child_server}/missing", timeout=0.3, interval=0.1, require_2xx=True
        )


def test_wait_for_child_undeclared_path_accepts_any_response(child_server):
    # No declared health route: a 404 still proves the server answers.
    adapter.wait_for_child(f"{child_server}/missing", timeout=5, require_2xx=False)


def test_live_health_accepts_declared_healthy_endpoint(child_server):
    assert adapter.check_child_health(
        f"{child_server}/healthz", timeout=1, require_2xx=True
    ) == (True, "HTTP 200")


def test_live_health_rejects_declared_unhealthy_endpoint(child_server):
    assert adapter.check_child_health(
        f"{child_server}/missing", timeout=1, require_2xx=True
    ) == (False, "HTTP 404")


def test_live_health_undeclared_path_accepts_any_response(child_server):
    assert adapter.check_child_health(
        f"{child_server}/missing", timeout=1, require_2xx=False
    ) == (True, "HTTP 404")


def test_live_health_timeout_is_bounded(monkeypatch):
    observed = []

    def unavailable(_url, timeout):
        observed.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(adapter.urllib.request, "urlopen", unavailable)
    assert (
        adapter.live_health_timeout({"MIGRATION_LIVE_HEALTH_TIMEOUT": "999"})
        == adapter.LIVE_HEALTH_TIMEOUT_MAX
    )
    assert adapter.check_child_health("http://127.0.0.1:1/", timeout=999) == (
        False,
        "TimeoutError: timed out",
    )
    assert observed == [adapter.LIVE_HEALTH_TIMEOUT_MAX]


# ── env merge ──
def test_child_env_legacy_fallback_merges_pairs_and_secrets():
    base = {"PATH": "/bin", "MIGRATION_ENV": "A=1,B=x=y"}
    child_env = adapter.build_child_env(base, {"JIRA_TOKEN": "s3cret"})
    assert child_env["PATH"] == "/bin"
    assert child_env["A"] == "1"
    assert child_env["B"] == "x=y"  # values may contain '='
    assert child_env["JIRA_TOKEN"] == "s3cret"
    assert "JIRA_TOKEN" not in base  # adapter's own env untouched


def test_child_env_json_preserves_commas_equals_and_secret_precedence():
    base = {
        "PATH": "/bin",
        "MIGRATION_ENV_JSON": json.dumps(
            {"OPTIONS": "a=b,c=d", "JIRA_TOKEN": "not-the-secret"}
        ),
    }
    child_env = adapter.build_child_env(base, {"JIRA_TOKEN": "s3cret"})
    assert child_env["OPTIONS"] == "a=b,c=d"
    assert child_env["JIRA_TOKEN"] == "s3cret"
    assert "OPTIONS" not in base


def test_child_env_json_is_preferred_over_legacy_pairs():
    environ = {
        "MIGRATION_ENV_JSON": '{"SOURCE":"json"}',
        "MIGRATION_ENV": "SOURCE=legacy,LEGACY_ONLY=yes",
    }
    child_env = adapter.build_child_env(environ, {})
    assert child_env["SOURCE"] == "json"
    assert "LEGACY_ONLY" not in child_env


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{not-json}",
        "[]",
        '"string"',
        '{"RETRIES": 3}',
        '{"ENABLED": true}',
        '{"EMPTY": null}',
    ],
)
def test_child_env_json_fails_closed_on_malformed_or_non_string_values(raw):
    with pytest.raises(
        ValueError,
        match="MIGRATION_ENV_JSON must be a valid JSON object of string values",
    ):
        adapter.build_child_env({"MIGRATION_ENV_JSON": raw}, {})


def test_parse_pairs_rejects_malformed_entries():
    with pytest.raises(ValueError, match="malformed"):
        adapter.parse_pairs("JUSTAKEY")


# ── secrets ──
class _ResourceNotFound(Exception):
    pass


class _FakeSecrets:
    def __init__(self, store):
        self.store = store

    def get_secret_value(self, SecretId):
        if SecretId not in self.store:
            raise _ResourceNotFound(f"ResourceNotFoundException: {SecretId}")
        return {"SecretString": self.store[SecretId]}


def test_secret_id_uses_platform_convention_and_arn_override():
    environ = {
        "PROJECT_NAME": "proj",
        "ENVIRONMENT": "dev",
        "MIGRATION_SECRET_ARNS": "GIT_TOKEN=arn:aws:secretsmanager:eu-central-1:123:secret:custom-abc",
    }
    assert (
        adapter.secret_id_for("JIRA_TOKEN", environ) == "proj/dev/migration/JIRA_TOKEN"
    )
    assert adapter.secret_id_for("GIT_TOKEN", environ).endswith("custom-abc")


def test_resolve_secrets_fetches_declared_names():
    environ = {
        "PROJECT_NAME": "proj",
        "ENVIRONMENT": "dev",
        "MIGRATION_SECRETS": "JIRA_TOKEN, GIT_TOKEN",
    }
    client = _FakeSecrets(
        {
            "proj/dev/migration/JIRA_TOKEN": "jira-value",
            "proj/dev/migration/GIT_TOKEN": "git-value",
        }
    )
    assert adapter.resolve_secrets(environ, client=client) == {
        "JIRA_TOKEN": "jira-value",
        "GIT_TOKEN": "git-value",
    }


def test_resolve_secrets_fails_fast_naming_the_missing_secret():
    environ = {
        "PROJECT_NAME": "proj",
        "ENVIRONMENT": "dev",
        "MIGRATION_SECRETS": "JIRA_TOKEN",
    }
    with pytest.raises(RuntimeError, match="proj/dev/migration/JIRA_TOKEN"):
        adapter.resolve_secrets(environ, client=_FakeSecrets({}))


def test_resolve_secrets_no_declarations_never_touches_aws():
    # client=None + no names: must return without importing/creating boto3.
    assert adapter.resolve_secrets({"MIGRATION_SECRETS": ""}) == {}


# ── runtime user ──
def test_runtime_user_rejects_lost_non_root_user():
    with pytest.raises(RuntimeError, match="running as root"):
        adapter.validate_runtime_user(
            {"ADAPTER_CHILD_USER": "appuser:appgroup"}, effective_uid=0
        )


def test_runtime_user_accepts_non_root_and_explicit_root():
    adapter.validate_runtime_user({"ADAPTER_CHILD_USER": "appuser"}, effective_uid=1000)
    adapter.validate_runtime_user({"ADAPTER_CHILD_USER": "root"}, effective_uid=0)


def test_runtime_user_rejects_numeric_uid_mismatch():
    with pytest.raises(RuntimeError, match="does not match"):
        adapter.validate_runtime_user(
            {"ADAPTER_CHILD_USER": "1001:1001"}, effective_uid=1000
        )


def test_dockerfile_restores_source_image_user():
    dockerfile = (REPO / "migration-adapter" / "Dockerfile").read_text()
    assert "ARG CHILD_USER=root" in dockerfile
    assert "ENV ADAPTER_CHILD_USER=${CHILD_USER}" in dockerfile
    assert "USER ${CHILD_USER}" in dockerfile
    assert dockerfile.rfind("USER ${CHILD_USER}") > dockerfile.find(
        "pip install --no-cache-dir"
    )
    assert "CA_BUNDLE_B64" not in dockerfile


# ── entrypoint guard ──
def test_invoke_reports_dead_child(monkeypatch):
    class DeadChild:
        def alive(self):
            return False

    monkeypatch.setattr(adapter, "_child", DeadChild())
    result = adapter.invoke({"prompt": "hi"})
    assert result["status"] == "error"
    assert result["code"] == 503


def test_invoke_rejects_unhealthy_live_child_without_forwarding(monkeypatch):
    class AliveChild:
        def alive(self):
            return True

    monkeypatch.setattr(adapter, "_child", AliveChild())
    monkeypatch.setenv("MIGRATION_HEALTH_PATH", "/healthz")
    monkeypatch.setattr(
        adapter,
        "check_child_health",
        lambda *_args, **_kwargs: (False, "HTTP 503"),
    )

    def must_not_forward(*_args, **_kwargs):
        pytest.fail("unhealthy invocation must not reach the child")

    monkeypatch.setattr(adapter, "forward", must_not_forward)
    result = adapter.invoke({"prompt": "hi"})
    assert result == {
        "status": "error",
        "code": 503,
        "error": "migrated agent health check failed (HTTP 503)",
    }


def test_invoke_preserves_forwarding_after_health_check(monkeypatch):
    class AliveChild:
        def alive(self):
            return True

    monkeypatch.setattr(adapter, "_child", AliveChild())
    monkeypatch.setattr(
        adapter,
        "check_child_health",
        lambda *_args, **_kwargs: (True, "HTTP 200"),
    )
    monkeypatch.setattr(
        adapter,
        "forward",
        lambda payload, url: {"payload": payload, "url": url},
    )
    assert adapter.invoke({"prompt": "hi"}) == {
        "payload": {"prompt": "hi"},
        "url": adapter._invoke_url(),
    }
