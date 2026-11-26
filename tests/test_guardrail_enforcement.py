"""Guardrailed-only Bedrock: the deny must never ship without the plumbing.

security.require_guardrails is one control with three moving parts — the IAM
Null-deny on the runtime role, the baseline guardrail whose id the runtime
injects, and the agent code that attaches it on model construction. The deny
alone bricks every pattern (each inference call comes back AccessDenied), so
these tests pin all three parts together, parsed from source — the CI test
job has no aws-cdk-lib.
"""

import ast
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml

from infra_utils.platform_config import PlatformConfig, to_env

RUNTIME_STACK = REPO / "stacks" / "runtime_stack.py"
GATEWAY_STACK = REPO / "stacks" / "gateway_stack.py"
GUARDRAIL_ARTIFACT = REPO / "control-library" / "guardrails" / "egress-default.json"
INTERCEPTOR = REPO / "tools" / "egress_interceptor" / "handler.py"
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


@pytest.mark.parametrize("agent", GUARDRAIL_CAPABLE_AGENTS)
def test_agents_never_default_to_draft(agent):
    source = (AGENT_CODE / agent / "agent.py").read_text()
    assert '"GUARDRAIL_VERSION", "DRAFT"' not in source
    assert "_guardrail_version()" in source


def _guardrail_version_fn(agent):
    path = AGENT_CODE / agent / "agent.py"
    tree = ast.parse(path.read_text())
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_guardrail_version"
    )
    namespace = {"os": os}
    module = ast.Module(body=[fn], type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)  # noqa: S102
    return namespace["_guardrail_version"]


@pytest.mark.parametrize("agent", GUARDRAIL_CAPABLE_AGENTS)
def test_missing_version_fails_fast(agent, monkeypatch):
    monkeypatch.delenv("GUARDRAIL_VERSION", raising=False)
    monkeypatch.setenv("GUARDRAIL_ALLOW_DRAFT", "true")
    with pytest.raises(RuntimeError):
        _guardrail_version_fn(agent)()


@pytest.mark.parametrize("agent", GUARDRAIL_CAPABLE_AGENTS)
def test_draft_needs_explicit_workshop_opt_in(agent, monkeypatch):
    fn = _guardrail_version_fn(agent)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.delenv("GUARDRAIL_ALLOW_DRAFT", raising=False)
    with pytest.raises(RuntimeError):
        fn()
    monkeypatch.setenv("GUARDRAIL_ALLOW_DRAFT", "true")
    assert fn() == "DRAFT"


@pytest.mark.parametrize("agent", GUARDRAIL_CAPABLE_AGENTS)
def test_production_ignores_allow_draft(agent, monkeypatch):
    fn = _guardrail_version_fn(agent)
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("GUARDRAIL_ALLOW_DRAFT", "true")
    with pytest.raises(RuntimeError):
        fn()
    monkeypatch.setenv("GUARDRAIL_VERSION", "3")
    assert fn() == "3"


@pytest.mark.parametrize("agent", GUARDRAIL_CAPABLE_AGENTS)
def test_published_version_is_used(agent, monkeypatch):
    monkeypatch.setenv("GUARDRAIL_VERSION", "3")
    monkeypatch.delenv("GUARDRAIL_ALLOW_DRAFT", raising=False)
    assert _guardrail_version_fn(agent)() == "3"


# ── published version + filter strength drift ──


@pytest.mark.parametrize("stack", [RUNTIME_STACK, GATEWAY_STACK])
def test_stacks_pass_the_published_version(stack):
    source = stack.read_text()
    assert "CfnGuardrailVersion" in source
    assert "guardrail_version.attr_version" in source
    assert "guardrail.attr_version" not in source, (
        "attr_version on the guardrail is DRAFT"
    )
    assert "sha256" in source, "the version must track a hash of the definition"


def test_runtime_stack_records_the_pinned_version():
    source = RUNTIME_STACK.read_text()
    assert "/version" in source and "SSMGuardrailVersion" in source


def test_prompt_attack_is_not_high():
    cfg = json.loads(GUARDRAIL_ARTIFACT.read_text())
    filters = cfg["ContentPolicyConfig"]["FiltersConfig"]
    prompt_attack = next(f for f in filters if f["Type"] == "PROMPT_ATTACK")
    assert prompt_attack["InputStrength"] == "MEDIUM"


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


# ── the enforcement check must respect the allow-list ──
# With allowed_models deployed, the BedrockModels allow is scoped; simulating
# against a generic model ARN returns implicitDeny even though real inference
# works — the check cried wolf the first time R1 and R2 ran together (live).


def _load_check_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_guardrail_enforcement",
        REPO / "scripts" / "check_guardrail_enforcement.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sim_arn_uses_the_allowlist(monkeypatch):
    monkeypatch.setenv("ALLOWED_MODELS", "us.anthropic.claude-sonnet-4-6")
    mod = _load_check_module()
    arn = mod.sim_model_arn()
    assert arn.endswith("::foundation-model/anthropic.claude-sonnet-4-6")
    assert "*" not in arn, "simulation needs a concrete ARN, not a pattern"


def test_sim_arn_falls_back_to_generic_without_a_list(monkeypatch, tmp_path):
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    # Point PLATFORM_CONFIG somewhere empty so a developer's local
    # platform.yaml can't leak into the test.
    monkeypatch.setenv("PLATFORM_CONFIG", str(tmp_path / "absent.yaml"))
    mod = _load_check_module()
    assert mod.sim_model_arn().endswith("::foundation-model/anthropic.claude")


# ── benign-prompt check ──


class _FakeBedrock:
    def __init__(self, action="NONE", params=None):
        self.action = action
        self.calls = []
        self.params = params or {}

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.params[Name]}}

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        return {"action": self.action}

    def list_guardrails(self, **kwargs):
        if "guardrailIdentifier" in kwargs:
            return {
                "guardrails": [
                    {"id": "gr-1", "version": v} for v in ("DRAFT", "2", "10")
                ]
            }
        return {"guardrails": [{"id": "gr-1", "name": "p-dev-orchestrator-guardrail"}]}


def test_benign_check_fails_when_a_greeting_is_blocked(monkeypatch):
    mod = _load_check_module()
    fake = _FakeBedrock("GUARDRAIL_INTERVENED")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: fake)
    with pytest.raises(SystemExit):
        mod.check_benign_prompts("gr-1", "1")


def test_benign_check_passes_and_uses_the_pinned_version(monkeypatch):
    mod = _load_check_module()
    fake = _FakeBedrock("NONE")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: fake)
    mod.check_benign_prompts("gr-1", "4")
    assert len(fake.calls) == len(mod.BENIGN_PROMPTS)
    assert all(c["guardrailVersion"] == "4" for c in fake.calls)
    assert all(c["source"] == "INPUT" for c in fake.calls)
    # Without guard_content, ApplyGuardrail skips the PROMPT_ATTACK filter.
    assert all(
        c["content"][0]["text"]["qualifiers"] == ["guard_content"] for c in fake.calls
    )


_PIN = "/p/dev/runtimes/orchestrator/guardrail"


def test_pinned_guardrail_uses_the_stack_pin_not_the_highest(monkeypatch):
    mod = _load_check_module()
    fake = _FakeBedrock(params={f"{_PIN}/id": "gr-1", f"{_PIN}/version": "2"})
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: fake)
    assert mod.pinned_guardrail("p", "dev") == ("gr-1", "2")


@pytest.mark.parametrize("version", ["DRAFT", "7"])
def test_pinned_guardrail_fails_on_draft_or_unpublished(monkeypatch, version):
    mod = _load_check_module()
    fake = _FakeBedrock(params={f"{_PIN}/id": "gr-1", f"{_PIN}/version": version})
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: fake)
    with pytest.raises(SystemExit):
        mod.pinned_guardrail("p", "dev")


# ── egress interceptor fails closed and uses one pinned version ──


def _load_interceptor(monkeypatch, **env):
    import importlib.util

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for key in (
        "GUARDRAIL_ID",
        "GUARDRAIL_VERSION",
        "GUARDRAIL_ALLOW_DRAFT",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("egress_handler", INTERCEPTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_interceptor_fails_closed_without_a_version(monkeypatch):
    with pytest.raises(RuntimeError):
        _load_interceptor(monkeypatch, GUARDRAIL_ID="gr-1")


def test_interceptor_refuses_draft_in_production(monkeypatch):
    with pytest.raises(RuntimeError):
        _load_interceptor(
            monkeypatch,
            GUARDRAIL_ID="gr-1",
            GUARDRAIL_VERSION="DRAFT",
            GUARDRAIL_ALLOW_DRAFT="true",
            ENVIRONMENT="prod",
        )


def test_interceptor_uses_the_pinned_version_for_every_call(monkeypatch):
    mod = _load_interceptor(
        monkeypatch, GUARDRAIL_ID="gr-1", GUARDRAIL_VERSION="7", ENVIRONMENT="prod"
    )
    fake = _FakeBedrock("NONE")
    monkeypatch.setattr(mod, "_bedrock", fake)
    mod.handler({"mcp": {"gatewayRequest": {"a": "hi", "b": ["there"]}}}, None)
    mod.handler({"mcp": {"gatewayResponse": {"c": "ok"}}}, None)
    assert len(fake.calls) == 3
    assert all(c["guardrailVersion"] == "7" for c in fake.calls)
    assert all(c["guardrailIdentifier"] == "gr-1" for c in fake.calls)
