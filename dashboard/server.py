#!/usr/bin/env python3
"""Loopback-only server for the local EBA Console.

The static dashboard remains usable with ``python -m http.server``. This
server adds one deliberately narrow capability: an operator can invoke an
already-deployed runtime from the Playground. It cannot create, update, or
delete AWS or Entra resources.
"""

from __future__ import annotations

import argparse
import ast
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
from collections import deque
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import boto3

REPO = Path(__file__).resolve().parents[1]
PUBLIC = REPO / "dashboard" / "public"
STATUS_FILE = PUBLIC / "status.json"
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPTS))

import invoke as runtime_invoke

from dashboard.monitor import load_config, runtime_context

logger = logging.getLogger("eba-console")

MAX_BODY_BYTES = 16 * 1024
MAX_PROMPT_CHARS = 4_000
MAX_INVOKE_RESPONSE_BYTES = 64 * 1024
MAX_RESPONSE_CHARS = 24_000
MAX_TRACE_EVENTS = 40
MAX_STATUS_AGE_SECONDS = 90
REQUESTS_PER_MINUTE = 20
MAX_CONCURRENT_INVOCATIONS = 2

COMPONENTS = {
    "orchestrator": "runtime-orchestrator",
    "code-agent": "runtime-code-agent",
    "research-agent": "runtime-research-agent",
}
INTERACTIVE_COMPONENT = "orchestrator"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
CSRF_TOKEN = secrets.token_urlsafe(32)


class RequestRejected(ValueError):
    """A safe request error that can be returned to the browser."""

    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class InvocationGate:
    """Bound concurrent work and per-process request rate."""

    def __init__(
        self,
        *,
        concurrent: int = MAX_CONCURRENT_INVOCATIONS,
        per_minute: int = REQUESTS_PER_MINUTE,
    ):
        self._slots = threading.BoundedSemaphore(concurrent)
        self._per_minute = per_minute
        self._history: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._history and self._history[0] <= now - 60:
                self._history.popleft()
            if len(self._history) >= self._per_minute:
                return False
            if not self._slots.acquire(blocking=False):
                return False
            self._history.append(now)
            return True

    def release(self) -> None:
        self._slots.release()


INVOCATION_GATE = InvocationGate()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_status(path: Path | None = None) -> dict:
    """Read a fresh, successful monitor snapshot or fail closed."""
    path = path or STATUS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Deployment status is unavailable. Start dashboard/monitor.py.",
        ) from exc
    if not isinstance(data, dict) or data.get("available") is not True:
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "The latest AWS status poll did not succeed. Check the monitor terminal.",
        )
    timestamp = _parse_timestamp(data.get("timestamp"))
    age = (datetime.now(UTC) - timestamp).total_seconds() if timestamp else float("inf")
    if age < -5 or age > MAX_STATUS_AGE_SECONDS:
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Deployment status is stale. Restart the monitor before invoking an agent.",
        )
    return data


def _stack_for_component(status: dict, component: str) -> dict | None:
    suffix = COMPONENTS[component]
    stacks = status.get("stacks")
    if not isinstance(stacks, dict):
        return None
    key = next((name for name in stacks if name.endswith(f"-{suffix}")), suffix)
    stack = stacks.get(key)
    return stack if isinstance(stack, dict) else None


def validate_invocation(payload: object, status: dict) -> tuple[str, str, str, str]:
    """Validate browser input and derive the protocol on the server."""
    if not isinstance(payload, dict):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "Request body must be an object.")
    component = payload.get("component")
    if component not in COMPONENTS:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "Unknown agent component.")
    if component != INTERACTIVE_COMPONENT:
        raise RequestRejected(
            HTTPStatus.FORBIDDEN,
            "A2A specialist runtimes are internal and cannot be invoked here.",
        )
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "Prompt is required.")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        raise RequestRejected(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"Prompt must be at most {MAX_PROMPT_CHARS} characters.",
        )
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{33,128}",
        session_id,
    ):
        raise RequestRejected(
            HTTPStatus.BAD_REQUEST,
            "Session ID must be 33–128 ASCII characters.",
        )
    stack = _stack_for_component(status, component)
    outputs = stack.get("outputs", {}) if stack else {}
    if (
        not stack
        or stack.get("state") != "deployed"
        or not isinstance(outputs, dict)
        or not outputs.get("RuntimeArn")
    ):
        raise RequestRejected(
            HTTPStatus.CONFLICT,
            "That runtime is not deployed in the current monitored footprint.",
        )
    return component, prompt, session_id, str(outputs["RuntimeArn"])


def validate_operator_identity(
    status: dict,
    runtime_arn: str,
    *,
    client_factory=boto3.client,
    context_factory=runtime_context,
) -> str:
    """Prove the server credentials and exact ARN match the monitor snapshot."""
    account = status.get("account")
    region = status.get("region")
    if not isinstance(account, str) or not re.fullmatch(r"\d{12}", account):
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Monitored AWS account is missing or invalid.",
        )
    if not isinstance(region, str) or not re.fullmatch(
        r"[a-z]{2}(?:-[a-z]+)+-\d",
        region,
    ):
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Monitored AWS Region is missing or invalid.",
        )
    try:
        context = context_factory()
    except Exception as exc:
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Could not load the EBA Console deployment configuration.",
        ) from exc
    if (
        context.project != status.get("project")
        or context.environment != status.get("environment")
        or context.region != region
    ):
        raise RequestRejected(
            HTTPStatus.CONFLICT,
            "The EBA Console configuration does not match the monitored deployment.",
        )
    arn = runtime_arn.split(":", 5)
    if (
        len(arn) != 6
        or arn[2] != "bedrock-agentcore"
        or arn[3] != region
        or arn[4] != account
        or not arn[5].startswith("runtime/")
    ):
        raise RequestRejected(
            HTTPStatus.CONFLICT,
            "The monitored runtime ARN does not match its account and Region.",
        )
    try:
        active_account = client_factory(
            "sts", region_name=region
        ).get_caller_identity()["Account"]
    except Exception as exc:
        raise RequestRejected(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Could not verify the EBA Console AWS identity.",
        ) from exc
    if active_account != account:
        raise RequestRejected(
            HTTPStatus.CONFLICT,
            "The EBA Console AWS profile does not match the monitored account.",
        )
    return region


def protocol_for(component: str) -> str:
    """Derive transport from the trusted manifest, never browser input."""
    if component != INTERACTIVE_COMPONENT:
        raise RequestRejected(
            HTTPStatus.FORBIDDEN,
            "Only the customer-facing orchestrator can be invoked here.",
        )
    config = load_config()
    if config.migration is not None:
        return "http"
    pattern = os.environ.get("AGENT_PATTERN") or config.agents.pattern
    return "ag-ui" if pattern.startswith("agui-") else "http"


def invoke_component(
    component: str,
    prompt: str,
    session_id: str,
    runtime_arn: str,
    region: str,
    *,
    invoke_module=runtime_invoke,
) -> tuple[str, str]:
    protocol = protocol_for(component)
    kwargs = {
        "runtime_arn": runtime_arn,
        "region": region,
        "max_response_bytes": MAX_INVOKE_RESPONSE_BYTES,
    }
    if protocol == "ag-ui":
        body = invoke_module.invoke_agui(prompt, session_id, **kwargs)
    else:
        body = invoke_module.invoke_agent(prompt, session_id, **kwargs)
    return protocol, body


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, dict) and item.get("text")
        )
    return ""


def _assistant_event_text(event: dict) -> str:
    """Extract only framework-defined assistant text fields.

    Never walk arbitrary nested JSON: tool results and diagnostic payloads can
    contain credentials or customer data that are not agent display text.
    """
    if event.get("type") == "TEXT_MESSAGE_CONTENT":
        return str(event.get("delta", ""))
    for key in ("response", "data"):
        if isinstance(event.get(key), str):
            return str(event[key])
    message = event.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return _content_text(message.get("content"))
    if str(event.get("type", "")).lower() in {
        "aimessage",
        "aimessagechunk",
        "assistant",
    }:
        return _content_text(event.get("content"))
    wrapped = event.get("event")
    if isinstance(wrapped, dict):
        return _assistant_event_text(wrapped)
    return ""


def _safe_trace_label(value: object, fallback: str) -> str:
    label = str(value or fallback).replace("\r", " ").replace("\n", " ").strip()
    return label[:160]


def parse_runtime_response(body: str) -> tuple[str, list[dict[str, str]]]:
    """Return bounded display text and metadata-only trace events."""
    body = body[:MAX_RESPONSE_CHARS]
    trace: list[dict[str, str]] = []
    text_deltas: list[str] = []

    if body.startswith("AGENT TEXT:"):
        raw = body.removeprefix("AGENT TEXT:").strip()
        try:
            value = ast.literal_eval(raw)
            output = value if isinstance(value, str) else str(value)
        except (SyntaxError, ValueError):
            output = raw
        return output[:MAX_RESPONSE_CHARS], trace

    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        text = _assistant_event_text(event)
        if text:
            text_deltas.append(text)
        if kind in {"TOOL_CALL_START", "TOOL_CALL_END", "TOOL_CALL_RESULT"}:
            trace.append(
                {
                    "kind": "tool",
                    "label": _safe_trace_label(
                        event.get("toolCallName"),
                        kind.replace("_", " ").title(),
                    ),
                }
            )
        elif kind == "RUN_ERROR":
            trace.append(
                {
                    "kind": "error",
                    "label": _safe_trace_label(event.get("code"), "Runtime error"),
                }
            )
        elif isinstance(event.get("current_tool_use"), dict):
            trace.append(
                {
                    "kind": "tool",
                    "label": _safe_trace_label(
                        event["current_tool_use"].get("name"),
                        "Tool call",
                    ),
                }
            )
        if len(trace) >= MAX_TRACE_EVENTS:
            break
    if text_deltas:
        return "".join(text_deltas)[:MAX_RESPONSE_CHARS], trace

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return "The runtime returned no displayable assistant text.", trace
    output = _assistant_event_text(decoded) if isinstance(decoded, dict) else ""
    return (
        (output or "The runtime returned no displayable assistant text.")[
            :MAX_RESPONSE_CHARS
        ],
        trace,
    )


def _loopback_host(value: str, expected_port: int) -> str | None:
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port or expected_port
    except ValueError:
        return None
    valid = (
        parsed.hostname in LOOPBACK_HOSTS
        and port == expected_port
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )
    return parsed.hostname if valid else None


def _is_loopback_authority(value: str, expected_port: int) -> bool:
    return _loopback_host(value, expected_port) is not None


def _is_same_origin(
    value: str,
    host_authority: str,
    expected_port: int,
) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
    except ValueError:
        return False
    request_host = _loopback_host(host_authority, expected_port)
    return (
        parsed.scheme == "http"
        and request_host is not None
        and parsed.hostname == request_host
        and port == expected_port
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


class EbaConsoleHandler(SimpleHTTPRequestHandler):
    """Static dashboard plus a same-origin invoke endpoint."""

    server_version = "AgentCoreEbaConsole/1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' "
            "'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'none'",
        )
        if self.path.startswith("/api/") or self.path.startswith("/status.json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _require_loopback_host(self) -> None:
        if not _is_loopback_authority(
            self.headers.get("Host", ""),
            self.server.server_port,
        ):
            raise RequestRejected(HTTPStatus.FORBIDDEN, "Loopback host required.")

    def _send_json(self, status: HTTPStatus, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            self._require_loopback_host()
            if self.path.split("?", 1)[0] == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "interactive": True,
                        "localOnly": True,
                        "maxPromptChars": MAX_PROMPT_CHARS,
                        "csrfToken": CSRF_TOKEN,
                    },
                )
                return
            super().do_GET()
        except RequestRejected as exc:
            self._send_json(exc.status, {"error": exc.message})

    def do_POST(self) -> None:
        acquired = False
        try:
            self._require_loopback_host()
            if self.path != "/api/invoke":
                raise RequestRejected(HTTPStatus.NOT_FOUND, "Endpoint not found.")
            if not _is_same_origin(
                self.headers.get("Origin", ""),
                self.headers.get("Host", ""),
                self.server.server_port,
            ):
                raise RequestRejected(HTTPStatus.FORBIDDEN, "Same origin required.")
            if not hmac.compare_digest(
                self.headers.get("X-EBA-CSRF", ""),
                CSRF_TOKEN,
            ):
                raise RequestRejected(
                    HTTPStatus.FORBIDDEN,
                    "Invalid EBA Console request token.",
                )
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                raise RequestRejected(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "Content-Type must be application/json.",
                )
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise RequestRejected(
                    HTTPStatus.LENGTH_REQUIRED,
                    "A valid Content-Length is required.",
                ) from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise RequestRejected(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"Request body must be between 1 and {MAX_BODY_BYTES} bytes.",
                )
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestRejected(
                    HTTPStatus.BAD_REQUEST,
                    "Request body must be valid JSON.",
                ) from exc

            status = load_status()
            component, prompt, session_id, runtime_arn = validate_invocation(
                payload,
                status,
            )
            region = validate_operator_identity(status, runtime_arn)
            if not INVOCATION_GATE.acquire():
                raise RequestRejected(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "The local invocation limit is busy. Wait and retry.",
                )
            acquired = True
            started = time.monotonic()
            protocol, raw = invoke_component(
                component,
                prompt,
                session_id,
                runtime_arn,
                region,
            )
            output, trace = parse_runtime_response(raw)
            self._send_json(
                HTTPStatus.OK,
                {
                    "component": component,
                    "protocol": protocol,
                    "sessionId": session_id,
                    "durationMs": round((time.monotonic() - started) * 1000),
                    "output": output,
                    "trace": trace,
                },
            )
        except RequestRejected as exc:
            self._send_json(exc.status, {"error": exc.message})
        except urllib.error.HTTPError as exc:
            logger.warning("runtime invocation returned HTTP %s", exc.code)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"Agent invocation failed with HTTP {exc.code}."},
            )
        except SystemExit:
            logger.warning("runtime invocation helper exited before completion")
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": (
                        "Agent invocation could not load its AWS configuration. "
                        "Check the local server terminal."
                    )
                },
            )
        except Exception as exc:  # noqa: BLE001 — stable browser boundary
            logger.warning("runtime invocation failed (%s)", type(exc).__name__)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": (
                        "Agent invocation failed. Check credentials and the "
                        "local server terminal."
                    )
                },
            )
        finally:
            if acquired:
                INVOCATION_GATE.release()

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.client_address[0], fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local AgentCore EBA Console")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), EbaConsoleHandler)
    print(f"EBA Console: http://127.0.0.1:{args.port}")
    print("Local operator access only. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
