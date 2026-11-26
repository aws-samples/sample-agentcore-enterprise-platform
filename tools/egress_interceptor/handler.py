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
  RESPONSE block:     same as REQUEST block (well-formed MCP isError result).

Only the body is scanned, and its JSON-RPC envelope (jsonrpc/id/method) is left intact.
Payload content is never logged: log lines carry only the interception point, the Lambda
request id, the JSON-RPC id, the body size and the decision.
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


_ENVELOPE_KEYS = frozenset({"jsonrpc", "id", "method"})
_BLOCKED_TEXT = "Blocked by egress guardrail."


def _mask_body(body, source: str):
    """Mask a JSON-RPC body, leaving its envelope (jsonrpc/id/method) untouched."""
    if isinstance(body, dict):
        return {
            k: v if k in _ENVELOPE_KEYS else _mask_tree(v, source)
            for k, v in body.items()
        }
    return _mask_tree(body, source)


def _rpc_id(*bodies):
    """JSON-RPC id of the first body that carries one."""
    for body in bodies:
        if isinstance(body, dict) and "id" in body:
            return body["id"]
    return None


def _size(body) -> int:
    return 0 if body is None else len(json.dumps(body, default=str))


def _log_decision(point, context, rpc_id, size, decision, reason=""):
    """Log ids, size and decision only; never payload content."""
    level = logging.WARNING if decision == "blocked" else logging.INFO
    logger.log(
        level,
        "egress interceptor point=%s request_id=%s rpc_id=%s body_bytes=%d "
        "decision=%s reason=%s",
        point,
        getattr(context, "aws_request_id", "-"),
        rpc_id,
        size,
        decision,
        reason or "-",
    )


def _output(key: str, value: dict) -> dict:
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": {key: value}}


def _blocked_gateway_response(rpc_id) -> dict:
    """A well-formed MCP tool result flagged isError, replacing the real payload."""
    return {
        "statusCode": 200,
        "body": {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": _BLOCKED_TEXT}],
            },
        },
    }


def _intercept_response(mcp: dict, context) -> dict:
    request_body = (mcp.get("gatewayRequest") or {}).get("body")
    response = mcp.get("gatewayResponse") or {}
    body = response.get("body")
    rpc_id = _rpc_id(body, request_body)
    try:
        masked = _mask_body(body, "OUTPUT")
    except GuardrailBlocked as exc:
        _log_decision("RESPONSE", context, rpc_id, _size(body), "blocked", str(exc))
        return _output("transformedGatewayResponse", _blocked_gateway_response(rpc_id))
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
    try:
        masked = _mask_body(body, "INPUT")
    except GuardrailBlocked as exc:
        _log_decision("REQUEST", context, rpc_id, _size(body), "blocked", str(exc))
        # Short-circuit: the target is never called.
        return _output("transformedGatewayResponse", _blocked_gateway_response(rpc_id))
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
