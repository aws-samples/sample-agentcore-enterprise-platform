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
request id, the JSON-RPC id (when short and scalar), the body size and the decision.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
_GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
_OUTPUT_VERSION = "1.0"

_bedrock = boto3.client("bedrock-runtime")


class GuardrailBlocked(Exception):
    """Raised when the guardrail hard-blocks content (e.g. prompt injection, blocked PII)."""


def _apply_guardrail(text: str, source: str) -> str:
    """Run the guardrail over one text value. Returns masked text, or raises GuardrailBlocked.

    source is "INPUT" (REQUEST) or "OUTPUT" (RESPONSE).
    """
    if not text.strip() or not _GUARDRAIL_ID:
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
    if isinstance(rpc_id, str) and len(rpc_id) <= _MAX_LOGGED_ID_LEN:
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
    request_body = (mcp.get("gatewayRequest") or {}).get("body")
    response = mcp.get("gatewayResponse") or {}
    body = response.get("body")
    rpc_id = _rpc_id(body, request_body)
    method = _method(request_body)
    try:
        masked = _mask_response_body(body)
    except GuardrailBlocked as exc:
        _log_decision("RESPONSE", context, rpc_id, _size(body), "blocked", str(exc))
        blocked = _blocked_gateway_response(rpc_id, method)
        return _output("transformedGatewayResponse", blocked)
    decision = "masked" if masked != body else "pass"
    _log_decision("RESPONSE", context, rpc_id, _size(body), decision)
    transformed = {"statusCode": response.get("statusCode", 200), "body": masked}
    if "headers" in response:
        transformed["headers"] = response["headers"]
    return _output("transformedGatewayResponse", transformed)


def _intercept_request(mcp: dict, context) -> dict:
    request = mcp.get("gatewayRequest") or {}
    body = request.get("body")
    rpc_id = _rpc_id(body)
    method = _method(body)
    try:
        masked = _mask_request_body(body, method)
    except GuardrailBlocked as exc:
        _log_decision("REQUEST", context, rpc_id, _size(body), "blocked", str(exc))
        # Short-circuit: the target is never called.
        blocked = _blocked_gateway_response(rpc_id, method)
        return _output("transformedGatewayResponse", blocked)
    decision = "masked" if masked != body else "pass"
    _log_decision("REQUEST", context, rpc_id, _size(body), decision)
    transformed = {"body": masked}
    if "headers" in request:
        transformed["headers"] = request["headers"]
    return _output("transformedGatewayRequest", transformed)


def handler(event, context):
    mcp = (event or {}).get("mcp") or {}

    # Response events carry gatewayRequest too, so check gatewayResponse first.
    if "gatewayResponse" in mcp:
        return _intercept_response(mcp, context)
    if "gatewayRequest" in mcp:
        return _intercept_request(mcp, context)

    # Unknown interception payload — pass through unchanged.
    _log_decision("UNKNOWN", context, None, 0, "pass", "no gateway payload")
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": mcp}
