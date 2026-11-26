# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AgentCore Gateway egress interceptor.

Applies a Bedrock Guardrail to Gateway traffic for defense-in-depth egress control:
  - REQUEST  interception point: guardrail runs with source=INPUT before the target is called
    (blocks prompt-injection, masks PII leaving the agent toward the tool/target).
  - RESPONSE interception point: guardrail runs with source=OUTPUT before the response returns
    to the caller (masks PII coming back from the target).

Interceptor contract (per AgentCore Gateway docs / CDK LambdaInterceptor):
  event  = {"mcp": {"gatewayRequest": {...}}}   for REQUEST
           {"mcp": {"gatewayResponse": {...}}}  for RESPONSE
  return = {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {...}}}   for REQUEST
           {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {...}}}  for RESPONSE

NOTE: the exact nested shape of gatewayRequest/gatewayResponse depends on the target type and
is treated generically here (all string leaves are scanned). Validate against live Gateway
traces before relying on it in production; the guardrail logic itself is target-agnostic.
"""

import json
import logging
import os

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


def _blocked_response(point: str, reason: str):
    """Return a safe, guardrail-blocked payload instead of the real request/response."""
    logger.warning("egress guardrail blocked (%s): %s", point, reason)
    key = (
        "transformedGatewayRequest"
        if point == "REQUEST"
        else "transformedGatewayResponse"
    )
    blocked = {"content": [{"type": "text", "text": "Blocked by egress guardrail."}]}
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": {key: blocked}}


def handler(event, context):
    logger.info("interceptor event: %s", json.dumps(event)[:2000])
    mcp = event.get("mcp", {}) or {}

    if "gatewayRequest" in mcp:
        try:
            transformed = _mask_tree(mcp["gatewayRequest"], "INPUT")
        except GuardrailBlocked as exc:
            return _blocked_response("REQUEST", str(exc))
        return {
            "interceptorOutputVersion": _OUTPUT_VERSION,
            "mcp": {"transformedGatewayRequest": transformed},
        }

    if "gatewayResponse" in mcp:
        try:
            transformed = _mask_tree(mcp["gatewayResponse"], "OUTPUT")
        except GuardrailBlocked as exc:
            return _blocked_response("RESPONSE", str(exc))
        return {
            "interceptorOutputVersion": _OUTPUT_VERSION,
            "mcp": {"transformedGatewayResponse": transformed},
        }

    # Unknown interception payload — pass through unchanged.
    logger.info("no gatewayRequest/gatewayResponse in event; passing through")
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": mcp}
