#!/usr/bin/env python3
"""Verify the guardrailed-only Bedrock claim: does IAM actually deny it?

Two policy simulations against the live orchestrator runtime role — no agent
invoke, no model call:

  1. bedrock:InvokeModel with NO bedrock:GuardrailIdentifier in the request
     context must come back denied: the DenyUngovernedInference statement's
     Null condition matches exactly when the key is absent.
  2. The same call WITH a guardrail identifier must come back allowed: the
     deny steps aside and the BedrockModels allow applies.

Exit code 0 = the role enforces what security.require_guardrails promises.
Run it after the runtime stacks are deployed with the flag on.

Usage:
    python scripts/check_guardrail_enforcement.py
"""

import argparse
import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import DEFAULT_ENV, DEFAULT_PROJECT

from infra_utils.platform_config import (
    allowed_model_resources,
    load_platform_config,
    resolve_region,
)

REGION = resolve_region()
REPO = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def sim_model_arn() -> str:
    """A foundation-model ARN the role's BedrockModels allow must match.

    With agents.allowed_models deployed, the allow statement is scoped to
    those models — a generic ARN then simulates to implicitDeny even though
    real inference works, and this check cries wolf (hit live the first time
    require_guardrails and the allow-list ran together). Resolution mirrors
    the deploy: ALLOWED_MODELS env > platform.yaml > wildcard-era generic.
    """
    models = [
        m.strip() for m in os.environ.get("ALLOWED_MODELS", "").split(",") if m.strip()
    ]
    if not models:
        manifest = Path(os.environ.get("PLATFORM_CONFIG") or REPO / "platform.yaml")
        if manifest.exists():
            try:
                models = load_platform_config(manifest).agents.allowed_models
            except Exception:  # noqa: BLE001, S110 — deploy validates loudly;
                pass  # a read-only check just falls back to the generic ARN.
    if models:
        # First allow-listed model's foundation-model ARN: concretize the
        # region wildcard and drop the trailing version wildcard.
        fm = next(
            r for r in allowed_model_resources(models[:1]) if ":foundation-model/" in r
        )
        return fm.replace("arn:aws:bedrock:*", f"arn:aws:bedrock:{REGION}", 1).rstrip(
            "*"
        )
    return f"arn:aws:bedrock:{REGION}::foundation-model/anthropic.claude"


def simulate(role_arn: str, context_entries: list[dict]) -> str:
    """One bedrock:InvokeModel simulation; returns the EvalDecision."""
    iam = boto3.client("iam", region_name=REGION)
    result = iam.simulate_principal_policy(
        PolicySourceArn=role_arn,
        ActionNames=["bedrock:InvokeModel"],
        ResourceArns=[sim_model_arn()],
        ContextEntries=context_entries,
    )
    return result["EvaluationResults"][0]["EvalDecision"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the runtime role denies inference without a guardrail"
    )
    parser.parse_args()

    project = os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
    environment = os.environ.get("ENVIRONMENT", DEFAULT_ENV)
    role_name = f"{project}-{environment}-orchestrator-runtime-role"
    print(f"Checking role {role_name} in {REGION}")

    iam = boto3.client("iam", region_name=REGION)
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        fail(f"role {role_name} not found — deploy the runtime stack first")

    decision = simulate(role_arn, [])
    if decision == "allowed":
        fail(
            "InvokeModel WITHOUT a guardrail came back allowed — the "
            "DenyUngovernedInference statement is missing or its Null condition "
            "is broken. Redeploy the runtime stack with require_guardrails=true."
        )
    print(f"PASS: inference without a guardrail is denied ({decision})")

    decision = simulate(
        role_arn,
        [
            {
                "ContextKeyName": "bedrock:GuardrailIdentifier",
                "ContextKeyValues": [
                    f"arn:aws:bedrock:{REGION}:123456789012:guardrail/gr-check"
                ],
                "ContextKeyType": "string",
            }
        ],
    )
    if decision != "allowed":
        fail(
            f"InvokeModel WITH a guardrail came back {decision} — the deny is "
            "over-matching (or the BedrockModels allow is gone), which bricks "
            "every agent instead of governing them."
        )
    print("PASS: inference with a guardrail is allowed")
    print("OK: the runtime role enforces guardrailed-only inference")


if __name__ == "__main__":
    main()
