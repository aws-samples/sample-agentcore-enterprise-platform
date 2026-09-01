"""Guardrailed-only Bedrock: the deny must never ship without the plumbing.

security.require_guardrails is one control with three moving parts — the IAM
Null-deny on the runtime role, the baseline guardrail whose id the runtime
injects, and the agent code that attaches it on model construction. The deny
alone bricks every pattern (each inference call comes back AccessDenied), so
these tests pin all three parts together, parsed from source — the CI test
job has no aws-cdk-lib.
"""

import ast
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml

from infra_utils.platform_config import PlatformConfig, to_env

RUNTIME_STACK = REPO / "stacks" / "runtime_stack.py"
AGENT_CODE = REPO / "agent-code"

# Every pattern that can attach a Bedrock Guardrail carries the env-gated
# plumbing; the claude-sdk patterns cannot, which is why the config validator
# refuses the combination outright.
GUARDRAIL_CAPABLE_AGENTS = (
    "orchestrator",
    "strands-agent",
    "agui-strands-agent",
    "code-agent",
    "research-agent",
    "langgraph-agent",
    "agui-langgraph-agent",
)
GUARDRAIL_INCAPABLE_AGENTS = ("claude-sdk-agent", "claude-sdk-multi-agent")


# ── config validation ──


@pytest.mark.parametrize("pattern", GUARDRAIL_INCAPABLE_AGENTS)
def test_claude_sdk_patterns_refuse_require_guardrails(pattern):
    with pytest.raises(ValidationError, match=pattern):
        PlatformConfig.model_validate(
            {
                "agents": {"pattern": pattern},
                "security": {"require_guardrails": True},
            }
        )


@pytest.mark.parametrize(
    "pattern", ["orchestrator", "strands-agent", "langgraph-agent"]
)
def test_guardrail_capable_patterns_validate(pattern):
    config = PlatformConfig.model_validate(
        {
            "agents": {"pattern": pattern},
            "security": {"require_guardrails": True},
        }
    )
    assert config.security.require_guardrails is True


def test_default_is_off():
    assert PlatformConfig().security.require_guardrails is False


def test_to_env_carries_the_flag():
    config = PlatformConfig.model_validate({"security": {"require_guardrails": True}})
    assert to_env(config)["REQUIRE_GUARDRAILS"] == "true"


# ── IAM source drift (same approach as test_runtime_role_scoping) ──


def _statement(sid: str) -> ast.Call:
    tree = ast.parse(RUNTIME_STACK.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "PolicyStatement"
            and any(
                kw.arg == "sid"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == sid
                for kw in node.keywords
            )
        ):
            return node
    raise AssertionError(f"no PolicyStatement with sid {sid!r} in runtime_stack.py")


def test_deny_ungoverned_inference_is_a_null_condition_deny():
    stmt = _statement("DenyUngovernedInference")
    kwargs = {kw.arg: kw.value for kw in stmt.keywords}
    assert kwargs["effect"].attr == "DENY", "the enforcement statement must deny"
    condition_src = ast.unparse(kwargs["conditions"])
    assert "'Null'" in condition_src
    assert "'bedrock:GuardrailIdentifier'" in condition_src


def test_apply_guardrail_is_scoped_to_the_baseline_guardrail():
    stmt = _statement("ApplyBaselineGuardrail")
    kwargs = {kw.arg: kw.value for kw in stmt.keywords}
    resources_src = ast.unparse(kwargs["resources"])
    assert "attr_guardrail_arn" in resources_src
    assert "'*'" not in resources_src, "ApplyGuardrail must not be wildcarded"


# ── agent plumbing drift ──


@pytest.mark.parametrize("agent", GUARDRAIL_CAPABLE_AGENTS)
def test_guardrail_capable_agents_read_the_injected_id(agent):
    source = (AGENT_CODE / agent / "agent.py").read_text()
    assert "GUARDRAIL_ID" in source, (
        f"{agent}/agent.py does not read GUARDRAIL_ID — with the IAM deny in "
        "place its every inference call comes back AccessDenied"
    )


@pytest.mark.parametrize("agent", GUARDRAIL_INCAPABLE_AGENTS)
def test_claude_sdk_agents_stay_untouched(agent):
    source = (AGENT_CODE / agent / "agent.py").read_text()
    assert "GUARDRAIL_ID" not in source, (
        f"{agent}/agent.py references GUARDRAIL_ID, but the Claude Agent SDK "
        "cannot attach a guardrail — the config validator (and this test) "
        "exist because that combination bricks the pattern"
    )


# ── preset ──


def test_security_focused_preset_enables_the_control():
    raw = yaml.safe_load((REPO / "presets" / "security-focused.yaml").read_text())
    config = PlatformConfig.model_validate(raw)
    assert config.security.require_guardrails is True
