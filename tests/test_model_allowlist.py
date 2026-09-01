"""The model allow-list must be enforceable, not decorative.

agents.allowed_models in platform.yaml does two things: validation refuses a
model_id outside the list, and the runtime role's Bedrock IAM statement is
scoped to exactly those models. Three invariants pinned here, each with a
failure mode that would otherwise surface only in a live account:

  - allowed_models without model_id must be rejected: when model_id is empty,
    MODEL_ID is never injected and each agent container falls back to its
    baked-in DEFAULT_MODEL_ID — the allow-list would be silently bypassed.
  - allowed_model_resources() must emit BOTH the inference-profile ARN and the
    base foundation-model ARN for geo-prefixed entries: invoking through a
    profile also needs foundation-model permissions in the target regions, so
    profile-only scoping breaks every invoke.
  - The shipped DEFAULT_MODEL_IDs must pass the allow-list validation, so a
    user CAN allow-list the defaults instead of being forced off them.

Pure Python, parsed from source where CDK would be needed — the CI test job
has no aws-cdk-lib.
"""

import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.platform_config import (
    AgentsConfig,
    allowed_model_resources,
)

RUNTIME_STACK = REPO / "stacks" / "runtime_stack.py"
SONNET = "us.anthropic.claude-sonnet-4-6"
OPUS = "us.anthropic.claude-opus-4-6-v1"


# ── Validation ──


def test_allowlist_without_model_id_is_rejected_as_a_bypass():
    with pytest.raises(ValidationError, match="bypass"):
        AgentsConfig(allowed_models=[SONNET])


def test_model_id_off_list_is_rejected():
    with pytest.raises(ValidationError, match="not in"):
        AgentsConfig(model_id=OPUS, allowed_models=[SONNET])


def test_model_id_on_list_is_valid():
    config = AgentsConfig(model_id=SONNET, allowed_models=[SONNET, OPUS])
    assert config.allowed_models == [SONNET, OPUS]


def test_empty_allowlist_keeps_todays_behavior():
    assert AgentsConfig(model_id=OPUS).allowed_models == []
    assert AgentsConfig().allowed_models == []


@pytest.mark.parametrize("bad", ["example.model", "not a model id!"])
def test_placeholder_and_garbage_entries_are_rejected(bad):
    with pytest.raises(ValidationError, match="not a Bedrock model id"):
        AgentsConfig(model_id=bad, allowed_models=[bad])


# ── ARN derivation ──


def test_geo_prefixed_entry_emits_profile_and_base_foundation_model():
    assert allowed_model_resources([SONNET]) == [
        f"arn:aws:bedrock:*:*:inference-profile/{SONNET}",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6*",
    ]


def test_plain_entry_emits_only_the_foundation_model_arn():
    assert allowed_model_resources(["anthropic.claude-opus-4-6-v1"]) == [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6-v1*"
    ]


def test_duplicates_dedupe_and_order_is_preserved():
    resources = allowed_model_resources([SONNET, OPUS, SONNET])
    assert len(resources) == len(set(resources))
    assert resources[0] == f"arn:aws:bedrock:*:*:inference-profile/{SONNET}"
    assert f"arn:aws:bedrock:*:*:inference-profile/{OPUS}" in resources


# ── IAM drift (intent, not synthesis — same approach as
#    test_runtime_role_scoping.test_ecr_pull_is_scoped_to_the_repository_arn) ──


def test_bedrock_statement_is_scoped_by_the_allowlist_with_wildcard_fallback():
    source = RUNTIME_STACK.read_text()
    assert "allowed_model_resources(allowed_models)" in source, (
        "the BedrockModels statement must derive its resources from the allow-list"
    )
    # The fallback (empty allow-list) must stay the historical wildcards so
    # existing deployments synthesize byte-identical templates.
    assert '"arn:aws:bedrock:*::foundation-model/*"' in source
    assert '"arn:aws:bedrock:*:*:inference-profile/*"' in source
    assert "from infra_utils.platform_config import allowed_model_resources" in source


# ── Defaults stay allow-listable ──


def _shipped_default_model_ids() -> set[str]:
    found = set()
    for agent in sorted((REPO / "agent-code").glob("*/agent.py")):
        match = re.search(
            r'^DEFAULT_MODEL_ID = "([^"]+)"', agent.read_text(), re.MULTILINE
        )
        if match:
            found.add(match.group(1))
    return found


def test_every_shipped_default_model_id_passes_the_allowlist():
    """A user must be able to allow-list the defaults the containers ship with.

    If a DEFAULT_MODEL_ID ever stops matching the field validator, setting
    allowed_models to the shipped defaults becomes impossible — the feature
    would force users onto other models.
    """
    defaults = _shipped_default_model_ids()
    assert defaults, "no DEFAULT_MODEL_ID found under agent-code/*/agent.py"
    for model in defaults:
        config = AgentsConfig(model_id=model, allowed_models=[model])
        assert config.allowed_models == [model]
        assert allowed_model_resources([model]), f"no ARNs derived for {model!r}"


# ── The security preset ships the allow-list on ──


def test_security_preset_pins_the_allowlist():
    """security-focused promises governed inference — dropping the pinned
    model + allow-list would silently widen the runtime IAM back to any model
    (owner decision 2026-08-31)."""
    from infra_utils.platform_config import load_platform_config

    config = load_platform_config(REPO / "presets" / "security-focused.yaml")
    assert config.agents.allowed_models == [SONNET]
    assert config.agents.model_id == SONNET
