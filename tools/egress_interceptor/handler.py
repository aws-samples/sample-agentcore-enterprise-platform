# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AgentCore Gateway egress interceptor.

Applies a Bedrock Guardrail to Gateway traffic for defense-in-depth egress control:
  - REQUEST  interception point: guardrail runs with source=INPUT before the target is called
    (blocks prompt-injection, masks PII leaving the agent toward the tool/target).
  - RESPONSE interception point: guardrail runs with source=OUTPUT before the response returns
    to the caller (masks PII coming back from the target).

Interceptor contract (AgentCore Gateway interceptors):
  REQUEST  event: {"mcp": {"gatewayRequest": {"body": {...JSON-RPC...}}}}
  RESPONSE event: {"mcp": {"gatewayRequest": {...}, "gatewayResponse":
                    {"statusCode": 200, "headers": {...}, "body": {...}}}}
  A response event carries BOTH keys, so gatewayResponse is checked first.

  REQUEST pass/mask:  {"interceptorOutputVersion": "1.0",
                       "mcp": {"transformedGatewayRequest": {"body": ...}}}
  REQUEST block:      short-circuit with
                      {"mcp": {"transformedGatewayResponse": {"statusCode": 200,
                               "body": <JSON-RPC result, isError=true>}}}
  RESPONSE pass/mask: {"mcp": {"transformedGatewayResponse":
                               {"statusCode": ..., "headers": ..., "body": ...}}}
  RESPONSE block:     same as REQUEST block.

  Block bodies depend on the request method: tools/call gets a well-formed tool result
  flagged isError; every other method gets a JSON-RPC error (code -32000), because an
  isError result lacks the fields those methods require (e.g. tools/list "tools").

Only content is scanned, never protocol fields: params.arguments of tools/call requests,
and result.content, result.structuredContent and error.message of responses. Tool names,
descriptions and input schemas are left untouched.
Payload content is never logged: log lines carry only the interception point, the Lambda
request id, the JSON-RPC id (when short and scalar), the body size and the decision, plus
one shape line per interception (method, body type, allow-listed top-level body and result
key names, tool count). Tool names, descriptions and content are never logged.

The body may arrive as a parsed map or as a JSON string. It is parsed for scanning and
returned in the type it arrived in, unchanged unless masked. Content-Length is dropped from
passed-through headers, because the gateway re-serializes the returned body.
"""

import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_OUTPUT_VERSION = "1.0"


def _pinned_guardrail() -> tuple[str, str]:
    """Return (id, version) of the published, pinned guardrail, or fail closed.

    Same rule as the agents: no fallback to DRAFT. DRAFT needs
    GUARDRAIL_ALLOW_DRAFT=true (workshop only), which is ignored when
    ENVIRONMENT is prod. A missing id or version fails the cold start, so no
    traffic passes the interceptor unscanned.
    """
    guardrail_id = os.environ.get("GUARDRAIL_ID", "").strip()
    version = os.environ.get("GUARDRAIL_VERSION", "").strip()
    environment = os.environ.get("ENVIRONMENT", "").strip().lower()
    allow_draft = (
        environment not in ("prod", "production")
        and os.environ.get("GUARDRAIL_ALLOW_DRAFT", "").lower() == "true"
    )
    if not guardrail_id:
        raise RuntimeError("GUARDRAIL_ID is not set; the interceptor fails closed")
    if not version or (version.upper() == "DRAFT" and not allow_draft):
        raise RuntimeError(
            "GUARDRAIL_VERSION is not a published version "
            f"(got {version!r}); DRAFT needs GUARDRAIL_ALLOW_DRAFT=true, which is "
            "workshop only and ignored when ENVIRONMENT is prod"
        )
    return guardrail_id, version


# Resolved once at cold start; every ApplyGuardrail call uses this same pin.
_GUARDRAIL_ID, _GUARDRAIL_VERSION = _pinned_guardrail()

_bedrock = boto3.client("bedrock-runtime")


class GuardrailBlocked(Exception):
    """Raised when the guardrail hard-blocks content (e.g. prompt injection, blocked PII)."""


def _apply_guardrail(text: str, source: str) -> str:
    """Run the guardrail over one text value. Returns masked text, or raises GuardrailBlocked.

    source is "INPUT" (REQUEST) or "OUTPUT" (RESPONSE).
    """
    if not text.strip():
        return text

    resp = _bedrock.apply_guardrail(
        guardrailIdentifier=_GUARDRAIL_ID,
        guardrailVersion=_GUARDRAIL_VERSION,
        source=source,
        content=[{"text": {"text": text}}],
    )

    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return text

    # Distinguish hard-block (deny) from anonymize (mask).
    for assessment in resp.get("assessments", []):
        # Prompt-attack / content filter blocks
        for f in assessment.get("contentPolicy", {}).get("filters", []):
            if f.get("action") == "BLOCKED":
                raise GuardrailBlocked(f"content filter {f.get('type')} blocked")
        # PII configured with BLOCK
        sens = assessment.get("sensitiveInformationPolicy", {})
        for pii in sens.get("piiEntities", []):
            if pii.get("action") == "BLOCKED":
                raise GuardrailBlocked(f"PII {pii.get('type')} blocked")

    # Otherwise it intervened by anonymizing — use the masked output text if present.
    outputs = resp.get("outputs") or []
    if outputs and outputs[0].get("text"):
        return outputs[0]["text"]
    return text


def _mask_tree(node, source: str):
    """Recursively apply the guardrail to every string leaf in a request/response object."""
    if isinstance(node, dict):
        return {k: _mask_tree(v, source) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_tree(v, source) for v in node]
    if isinstance(node, str):
        return _apply_guardrail(node, source)
    return node


_RESPONSE_CONTENT_KEYS = frozenset({"content", "structuredContent"})
_BLOCKED_TEXT = "Blocked by egress guardrail."
_BLOCKED_ERROR_CODE = -32000
_MAX_LOGGED_ID_LEN = 64
_SAFE_ID_RE = re.compile(rf"[A-Za-z0-9._:/-]{{1,{_MAX_LOGGED_ID_LEN}}}")
_METHOD_RE = re.compile(r"[A-Za-z_]{1,32}(?:/[A-Za-z_]{1,32}){0,3}")
# Only protocol key names are logged; any other key is counted, never named.
_LOGGABLE_KEYS = frozenset(
    {
        "jsonrpc",
        "id",
        "method",
        "params",
        "result",
        "error",
        "tools",
        "nextCursor",
        "content",
        "structuredContent",
        "isError",
        "_meta",
    }
)


def _parse_body(body):
    """Return (parsed, was_string). A body that is not JSON text is returned as is."""
    if isinstance(body, (str, bytes)):
        try:
            return json.loads(body), True
        except ValueError:
            return body, False
    return body, False


def _restore_body(raw, parsed, masked, was_string):
    """Return the original body when unchanged, else the masked body in its type."""
    if masked == parsed:
        return raw
    return json.dumps(masked) if was_string else masked


def _without_content_length(headers):
    """Drop Content-Length: the gateway re-serializes the returned body."""
    if not isinstance(headers, dict):
        return headers
    return {
        k: v
        for k, v in headers.items()
        if not (isinstance(k, str) and k.lower() == "content-length")
    }


def _loggable_method(method) -> str:
    if method is None:
        return "-"
    if isinstance(method, str) and _METHOD_RE.fullmatch(method):
        return method
    return "<other>"


def _loggable_keys(node) -> str:
    """Allow-listed key names of a dict; other keys only as a count."""
    if not isinstance(node, dict):
        return "-"
    known = sorted(k for k in node if k in _LOGGABLE_KEYS)
    other = len(node) - len(known)
    if other:
        known.append(f"<other:{other}>")
    return ",".join(known) or "-"


def _log_shape(point, context, method, raw, parsed, streaming=False):
    """Log non-payload shape metadata only; never names, descriptions or content."""
    result = parsed.get("result") if isinstance(parsed, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    logger.info(
        "egress interceptor shape point=%s request_id=%s method=%s body_type=%s "
        "body_keys=%s result_keys=%s tools=%s streaming=%s",
        point,
        getattr(context, "aws_request_id", "-"),
        _loggable_method(method),
        type(raw).__name__,
        _loggable_keys(parsed),
        _loggable_keys(result),
        len(tools) if isinstance(tools, list) else "-",
        bool(streaming),
    )


def _method(body):
    """JSON-RPC method of a request body, or None."""
    if isinstance(body, dict) and isinstance(body.get("method"), str):
        return body["method"]
    return None


def _mask_request_body(body, method):
    """Mask only tools/call params.arguments; protocol fields are never scanned."""
    if method != "tools/call" or not isinstance(body, dict):
        return body
    params = body.get("params")
    if not isinstance(params, dict) or "arguments" not in params:
        return body
    arguments = _mask_tree(params["arguments"], "INPUT")
    return {**body, "params": {**params, "arguments": arguments}}


def _mask_response_body(body):
    """Mask only result.content, result.structuredContent and error.message."""
    if not isinstance(body, dict):
        return body
    masked = dict(body)
    result = body.get("result")
    if isinstance(result, dict):
        masked["result"] = {
            k: _mask_tree(v, "OUTPUT") if k in _RESPONSE_CONTENT_KEYS else v
            for k, v in result.items()
        }
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        message = _apply_guardrail(error["message"], "OUTPUT")
        masked["error"] = {**error, "message": message}
    return masked


def _rpc_id(*bodies):
    """JSON-RPC id of the first body that carries one."""
    for body in bodies:
        if isinstance(body, dict) and "id" in body:
            return body["id"]
    return None


def _size(body) -> int:
    return 0 if body is None else len(json.dumps(body, default=str))


def _loggable_id(rpc_id) -> str:
    """The client picks the id, so log it only when it is an int or a short string."""
    if rpc_id is None:
        return "-"
    if isinstance(rpc_id, int) and not isinstance(rpc_id, bool):
        return str(rpc_id)
    # Printable id characters only: a newline or control character in a
    # client-chosen id would otherwise forge log lines.
    if isinstance(rpc_id, str) and _SAFE_ID_RE.fullmatch(rpc_id):
        return rpc_id
    return f"<{type(rpc_id).__name__} len={_size(rpc_id)}>"


def _log_decision(point, context, rpc_id, size, decision, reason=""):
    """Log ids, size and decision only; never payload content."""
    level = logging.WARNING if decision == "blocked" else logging.INFO
    logger.log(
        level,
        "egress interceptor point=%s request_id=%s rpc_id=%s body_bytes=%d "
        "decision=%s reason=%s",
        point,
        getattr(context, "aws_request_id", "-"),
        _loggable_id(rpc_id),
        size,
        decision,
        reason or "-",
    )


def _output(key: str, value: dict) -> dict:
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": {key: value}}


def _blocked_gateway_response(rpc_id, method) -> dict:
    """Blocked reply valid for the request method, replacing the real payload.

    tools/call gets a tool result flagged isError; any other method gets a JSON-RPC
    error, since an isError result lacks that method's required result fields.
    """
    if method == "tools/call":
        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": _BLOCKED_TEXT}],
            },
        }
    else:
        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": _BLOCKED_ERROR_CODE, "message": _BLOCKED_TEXT},
        }
    return {"statusCode": 200, "body": body}


def _intercept_response(mcp: dict, context) -> dict:
    request_body, _ = _parse_body((mcp.get("gatewayRequest") or {}).get("body"))
    response = mcp.get("gatewayResponse") or {}
    raw = response.get("body")
    body, was_string = _parse_body(raw)
    rpc_id = _rpc_id(body, request_body)
    method = _method(request_body)
    streaming = response.get("isStreamingResponse", mcp.get("isStreamingResponse"))
    _log_shape("RESPONSE", context, method, raw, body, streaming)
    try:
        masked = _mask_response_body(body)
    except GuardrailBlocked as exc:
        _log_decision("RESPONSE", context, rpc_id, _size(raw), "blocked", str(exc))
        blocked = _blocked_gateway_response(rpc_id, method)
        return _output("transformedGatewayResponse", blocked)
    decision = "masked" if masked != body else "pass"
    _log_decision("RESPONSE", context, rpc_id, _size(raw), decision)
    transformed = {
        "statusCode": response.get("statusCode", 200),
        "body": _restore_body(raw, body, masked, was_string),
    }
    if "headers" in response:
        transformed["headers"] = _without_content_length(response["headers"])
    return _output("transformedGatewayResponse", transformed)


def _intercept_request(mcp: dict, context) -> dict:
    request = mcp.get("gatewayRequest") or {}
    raw = request.get("body")
    body, was_string = _parse_body(raw)
    rpc_id = _rpc_id(body)
    method = _method(body)
    _log_shape("REQUEST", context, method, raw, body)
    try:
        masked = _mask_request_body(body, method)
    except GuardrailBlocked as exc:
        _log_decision("REQUEST", context, rpc_id, _size(raw), "blocked", str(exc))
        # Short-circuit: the target is never called.
        blocked = _blocked_gateway_response(rpc_id, method)
        return _output("transformedGatewayResponse", blocked)
    decision = "masked" if masked != body else "pass"
    _log_decision("REQUEST", context, rpc_id, _size(raw), decision)
    transformed = {"body": _restore_body(raw, body, masked, was_string)}
    if "headers" in request:
        transformed["headers"] = _without_content_length(request["headers"])
    return _output("transformedGatewayRequest", transformed)


def handler(event, context):
    mcp = (event or {}).get("mcp") or {}

    # Response events carry gatewayRequest too, so check gatewayResponse first.
    # Only a non-empty gatewayResponse marks a RESPONSE interception: live,
    # REQUEST events arrive with "gatewayResponse": null. Treating those as
    # responses returned transformedGatewayResponse with an empty body, which
    # the gateway sends straight back, so tools/list came back with no tools.
    if mcp.get("gatewayResponse"):
        return _intercept_response(mcp, context)
    if "gatewayRequest" in mcp:
        return _intercept_request(mcp, context)

    # Unknown interception payload — pass through unchanged.
    _log_decision("UNKNOWN", context, None, 0, "pass", "no gateway payload")
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": mcp}
