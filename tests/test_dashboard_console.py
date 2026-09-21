"""Security and behavior tests for the local EBA Console."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dashboard import server

CONSOLE_JS = (REPO / "dashboard" / "public" / "console.js").read_text()
INDEX_HTML = (REPO / "dashboard" / "public" / "index.html").read_text()


def _status(
    *,
    component: str = "orchestrator",
    state: str = "deployed",
    age_seconds: int = 0,
) -> dict:
    suffix = server.COMPONENTS[component]
    return {
        "available": True,
        "timestamp": (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat(),
        "account": "111111111111",
        "region": "eu-west-1",
        "project": "project",
        "environment": "dev",
        "stacks": {
            f"project-dev-{suffix}": {
                "state": state,
                "outputs": {
                    "RuntimeArn": (
                        "arn:aws:bedrock-agentcore:eu-west-1:111111111111:runtime/test"
                    ),
                    "RuntimeId": "runtime-id",
                },
            }
        },
    }


def test_status_must_be_fresh_and_successful(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(_status(age_seconds=server.MAX_STATUS_AGE_SECONDS + 1)))
    with pytest.raises(server.RequestRejected, match="stale"):
        server.load_status(path)

    path.write_text(json.dumps({"available": False}))
    with pytest.raises(server.RequestRejected, match="did not succeed"):
        server.load_status(path)


def test_invocation_accepts_only_deployed_allowlisted_components():
    component, prompt, session, runtime_arn = server.validate_invocation(
        {
            "component": "orchestrator",
            "prompt": "  synthetic question  ",
            "sessionId": "session-" + ("a" * 32),
            # Browser-supplied routing fields are ignored.
            "runtimeArn": "arn:attacker:selected",
            "protocol": "attacker-selected",
        },
        _status(),
    )
    assert (component, prompt, session) == (
        "orchestrator",
        "synthetic question",
        "session-" + ("a" * 32),
    )
    assert runtime_arn == (
        "arn:aws:bedrock-agentcore:eu-west-1:111111111111:runtime/test"
    )

    with pytest.raises(server.RequestRejected, match="Unknown"):
        server.validate_invocation(
            {
                "component": "arbitrary-runtime",
                "prompt": "hello",
                "sessionId": "session-" + ("a" * 32),
            },
            _status(),
        )
    with pytest.raises(server.RequestRejected, match="not deployed"):
        server.validate_invocation(
            {
                "component": "orchestrator",
                "prompt": "hello",
                "sessionId": "session-" + ("a" * 32),
            },
            _status(state="failed"),
        )
    with pytest.raises(server.RequestRejected, match="internal"):
        server.validate_invocation(
            {
                "component": "code-agent",
                "prompt": "hello",
                "sessionId": "session-" + ("a" * 32),
            },
            _status(component="code-agent"),
        )


def test_invocation_rejects_oversized_prompt_and_unsafe_session():
    with pytest.raises(server.RequestRejected, match="at most"):
        server.validate_invocation(
            {
                "component": "orchestrator",
                "prompt": "x" * (server.MAX_PROMPT_CHARS + 1),
                "sessionId": "session-" + ("a" * 32),
            },
            _status(),
        )
    with pytest.raises(server.RequestRejected, match="Session ID"):
        server.validate_invocation(
            {
                "component": "orchestrator",
                "prompt": "hello",
                "sessionId": ("a" * 40) + "\n",
            },
            _status(),
        )


@pytest.mark.parametrize(
    ("component", "pattern", "migration", "expected"),
    [
        ("orchestrator", "orchestrator", None, "http"),
        ("orchestrator", "agui-strands-agent", None, "ag-ui"),
        ("orchestrator", "agui-strands-agent", object(), "http"),
    ],
)
def test_protocol_is_derived_from_trusted_config(
    monkeypatch,
    component,
    pattern,
    migration,
    expected,
):
    monkeypatch.delenv("AGENT_PATTERN", raising=False)
    monkeypatch.setattr(
        server,
        "load_config",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(pattern=pattern),
            migration=migration,
        ),
    )
    assert server.protocol_for(component) == expected


class _STS:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


def test_operator_identity_and_runtime_must_match_monitor():
    status = _status()
    arn = status["stacks"]["project-dev-runtime-orchestrator"]["outputs"]["RuntimeArn"]

    assert (
        server.validate_operator_identity(
            status,
            arn,
            client_factory=lambda service, region_name: _STS("111111111111"),
            context_factory=lambda: SimpleNamespace(
                project="project",
                environment="dev",
                region="eu-west-1",
            ),
        )
        == "eu-west-1"
    )
    with pytest.raises(server.RequestRejected, match="profile does not match"):
        server.validate_operator_identity(
            status,
            arn,
            client_factory=lambda service, region_name: _STS("222222222222"),
            context_factory=lambda: SimpleNamespace(
                project="project",
                environment="dev",
                region="eu-west-1",
            ),
        )
    with pytest.raises(server.RequestRejected, match="runtime ARN"):
        server.validate_operator_identity(
            status,
            arn.replace("eu-west-1", "us-east-1"),
            client_factory=lambda service, region_name: _STS("111111111111"),
            context_factory=lambda: SimpleNamespace(
                project="project",
                environment="dev",
                region="eu-west-1",
            ),
        )
    with pytest.raises(server.RequestRejected, match="configuration"):
        server.validate_operator_identity(
            status,
            arn,
            client_factory=lambda service, region_name: _STS("111111111111"),
            context_factory=lambda: SimpleNamespace(
                project="another-project",
                environment="dev",
                region="eu-west-1",
            ),
        )


def test_console_invokes_the_exact_monitored_arn(monkeypatch):
    monkeypatch.setattr(
        server,
        "load_config",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(pattern="orchestrator"),
            migration=None,
        ),
    )
    calls = []
    fake = SimpleNamespace(
        invoke_agent=lambda prompt, session, **kwargs: (
            calls.append((prompt, session, kwargs)) or '{"response":"ok"}'
        )
    )
    arn = "arn:aws:bedrock-agentcore:eu-west-1:111111111111:runtime/exact"
    protocol, _ = server.invoke_component(
        "orchestrator",
        "hello",
        "session-" + ("a" * 32),
        arn,
        "eu-west-1",
        invoke_module=fake,
    )
    assert protocol == "http"
    assert calls[0][2] == {
        "runtime_arn": arn,
        "region": "eu-west-1",
        "max_response_bytes": server.MAX_INVOKE_RESPONSE_BYTES,
    }


def test_runtime_response_exposes_text_and_metadata_only_trace():
    body = (
        'data: {"type":"TOOL_CALL_START","toolCallName":"search",'
        '"toolCallId":"1","args":"must-not-appear"}\n'
        'data: {"type":"TEXT_MESSAGE_CONTENT","delta":"Hello "}\n'
        'data: {"type":"TEXT_MESSAGE_CONTENT","delta":"world"}'
    )
    output, trace = server.parse_runtime_response(body)
    assert output == "Hello world"
    assert trace == [{"kind": "tool", "label": "search"}]
    assert "must-not-appear" not in json.dumps(trace)


def test_agent_text_wrappers_are_normalized():
    assert server.parse_runtime_response("AGENT TEXT: 'hello'") == ("hello", [])
    assert server.parse_runtime_response("AGENT TEXT: hello") == ("hello", [])


def test_unknown_payload_and_tool_result_never_render_raw_canary():
    canary = "SECRET-CANARY-MUST-NOT-RENDER"
    body = json.dumps(
        {
            "message": {
                "role": "user",
                "content": [{"toolResult": {"content": [{"text": canary}]}}],
            }
        }
    )
    output, trace = server.parse_runtime_response(body)
    assert output == "The runtime returned no displayable assistant text."
    assert canary not in output
    assert canary not in json.dumps(trace)


def test_runtime_reader_rejects_oversized_response():
    response = BytesIO(b"x" * 9)
    with pytest.raises(
        server.runtime_invoke.RuntimeVerificationError, match="exceeded"
    ):
        server.runtime_invoke._read_bounded(response, limit=8)


@pytest.fixture
def console_handler(monkeypatch):
    monkeypatch.setattr(server, "load_status", _status)
    monkeypatch.setattr(
        server,
        "validate_operator_identity",
        lambda status, runtime_arn: "eu-west-1",
    )
    monkeypatch.setattr(
        server,
        "invoke_component",
        lambda component, prompt, session, runtime_arn, region: (
            "http",
            json.dumps({"response": f"reply to {prompt}"}),
        ),
    )

    def request(method: str, path: str, body: bytes = b"", headers=None):
        port = 8888
        handler = server.EbaConsoleHandler.__new__(server.EbaConsoleHandler)
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.server = SimpleNamespace(server_port=port)
        handler.client_address = ("127.0.0.1", 12345)
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.headers = Message()
        all_headers = {
            "Host": f"127.0.0.1:{port}",
            **(headers or {}),
        }
        if body and "Content-Length" not in all_headers:
            all_headers["Content-Length"] = str(len(body))
        for key, value in all_headers.items():
            handler.headers[key] = value
        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()
        raw_headers, response_body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        lines = raw_headers.decode().splitlines()
        status = int(lines[0].split()[1])
        response_headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                response_headers[key] = value.strip()
        return status, response_headers, response_body

    return request


def test_health_endpoint_is_local_and_hardened(console_handler):
    status, headers, body = console_handler("GET", "/api/health")
    assert status == 200
    health = json.loads(body)
    assert health["interactive"] is True
    assert health["localOnly"] is True
    assert health["maxPromptChars"] == server.MAX_PROMPT_CHARS
    assert health["csrfToken"] == server.CSRF_TOKEN
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"

    status, _, _ = console_handler(
        "GET",
        "/api/health",
        headers={"Host": "attacker.example"},
    )
    assert status == 403

    assert not server._is_same_origin(
        "http://localhost:8888",
        "127.0.0.1:8888",
        8888,
    )


def test_post_requires_same_origin_json_and_never_returns_raw(console_handler):
    payload = json.dumps(
        {
            "component": "orchestrator",
            "prompt": "synthetic question",
            "sessionId": "session-" + ("a" * 32),
        }
    ).encode()
    status, _, _ = console_handler(
        "POST",
        "/api/invoke",
        body=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://attacker.example",
            "X-EBA-CSRF": server.CSRF_TOKEN,
        },
    )
    assert status == 403

    status, _, body = console_handler(
        "POST",
        "/api/invoke",
        body=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8888",
            "X-EBA-CSRF": server.CSRF_TOKEN,
        },
    )
    response = json.loads(body)
    assert status == 200
    assert response["output"] == "reply to synthetic question"
    assert response["component"] == "orchestrator"
    assert "raw" not in response


def test_no_cloud_mutation_endpoint_is_exposed(console_handler):
    payload = b"{}"
    for path in ("/api/create", "/api/delete", "/api/agents"):
        status, _, _ = console_handler(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8888",
                "X-EBA-CSRF": server.CSRF_TOKEN,
            },
        )
        assert status == 404


def test_post_rejects_missing_csrf_token(console_handler):
    payload = json.dumps(
        {
            "component": "orchestrator",
            "prompt": "synthetic question",
            "sessionId": "session-" + ("a" * 32),
        }
    ).encode()
    status, _, body = console_handler(
        "POST",
        "/api/invoke",
        body=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8888",
        },
    )
    assert status == 403
    assert "token" in json.loads(body)["error"]


def test_helper_system_exit_becomes_stable_browser_error(
    console_handler,
    monkeypatch,
):
    monkeypatch.setattr(
        server,
        "invoke_component",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(1)),
    )
    payload = json.dumps(
        {
            "component": "orchestrator",
            "prompt": "synthetic question",
            "sessionId": "session-" + ("a" * 32),
        }
    ).encode()
    status, _, body = console_handler(
        "POST",
        "/api/invoke",
        body=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8888",
            "X-EBA-CSRF": server.CSRF_TOKEN,
        },
    )
    assert status == 502
    assert json.loads(body)["error"].startswith("Agent invocation could not")


def test_ui_preserves_the_demo_style_without_unsafe_response_rendering():
    assert 'class="sidebar"' in INDEX_HTML
    assert 'id="tab-agents"' in INDEX_HTML
    assert 'id="tab-playground"' in INDEX_HTML
    assert "Use synthetic or customer-approved data only" in INDEX_HTML
    assert "createElement" in CONSOLE_JS
    assert ".textContent =" in CONSOLE_JS
    assert "innerHTML" not in CONSOLE_JS
    assert "navigator.clipboard" not in CONSOLE_JS
    assert "create agent" not in CONSOLE_JS.lower()
    assert "delete agent" not in CONSOLE_JS.lower()
    assert "Math.random" not in CONSOLE_JS
    assert "getRandomValues" in CONSOLE_JS
    assert '"X-EBA-CSRF"' in CONSOLE_JS
    assert "agent.customerFacing" in CONSOLE_JS
