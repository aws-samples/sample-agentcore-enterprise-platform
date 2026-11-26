"""Long-term memory isolation: per-actor namespaces and retrieval config."""

import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks import memory_stack
from stacks.memory_stack import MemoryStack

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ("strands-agent", "agui-strands-agent")


def _load_jwt_claims():
    spec = importlib.util.spec_from_file_location(
        "_jwt_claims_memory_under_test",
        ROOT / "agent-code" / "shared" / "jwt_claims.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jwt_claims = _load_jwt_claims()


def _namespaces(use_ltm: bool) -> list[str]:
    app = cdk.App()
    stack = MemoryStack(
        app,
        "mem",
        project_name="p",
        environment="dev",
        use_long_term_memory=use_ltm,
    )
    resources = Template.from_stack(stack).find_resources(
        "AWS::BedrockAgentCore::Memory"
    )
    (memory,) = resources.values()
    namespaces = []
    for strategy in memory["Properties"]["MemoryStrategies"]:
        for body in strategy.values():
            namespaces.extend(body["Namespaces"])
    return namespaces


@pytest.mark.parametrize("use_ltm", [False, True])
def test_every_synthesized_namespace_is_per_actor(use_ltm):
    namespaces = _namespaces(use_ltm)
    assert len(namespaces) == (2 if use_ltm else 1)
    for namespace in namespaces:
        assert "{actorId}" in namespace
        assert namespace not in ("USER_ID", "AGENT_ID")


def test_long_term_memory_adds_facts_namespace():
    assert set(_namespaces(True)) == {
        "/users/{actorId}/preferences",
        "/users/{actorId}/facts",
    }


def test_stack_and_agent_templates_agree():
    assert (
        memory_stack.USER_PREFERENCE_NAMESPACE == jwt_claims.LTM_PREFERENCES_NAMESPACE
    )
    assert memory_stack.SEMANTIC_NAMESPACE == jwt_claims.LTM_FACTS_NAMESPACE


def test_memory_stack_has_no_dead_retrieval_parameters():
    params = inspect.signature(MemoryStack.__init__).parameters
    assert "ltm_top_k" not in params
    assert "ltm_relevance_score" not in params


def test_retrieval_plan_from_env_and_sub():
    env = {
        "LTM_TOP_K": "4",
        "LTM_RELEVANCE_SCORE": "0.55",
        "USE_LONG_TERM_MEMORY": "true",
    }
    plan = jwt_claims.memory_retrieval_plan("sub-123", env)
    assert plan == {
        "/users/sub-123/preferences": {"top_k": 4, "relevance_score": 0.55},
        "/users/sub-123/facts": {"top_k": 4, "relevance_score": 0.55},
    }


def test_retrieval_plan_defaults_and_no_facts_without_ltm():
    plan = jwt_claims.memory_retrieval_plan("sub-123", {})
    assert plan == {
        "/users/sub-123/preferences": {"top_k": 10, "relevance_score": 0.3},
    }


@pytest.mark.parametrize("actor_id", ["", "a/b", "../x", "{actorId}", "a b", None])
def test_unsafe_actor_id_rejected(actor_id):
    with pytest.raises(ValueError):
        jwt_claims.build_retrieval_namespaces(actor_id)


@pytest.mark.parametrize(
    "env",
    [
        {"LTM_TOP_K": "many"},
        {"LTM_TOP_K": "0"},
        {"LTM_RELEVANCE_SCORE": "high"},
        {"LTM_RELEVANCE_SCORE": "1.5"},
        {"LTM_RELEVANCE_SCORE": "nan"},
    ],
)
def test_invalid_retrieval_settings_fall_back_with_warning(env, caplog):
    caplog.set_level(logging.WARNING)
    assert jwt_claims.ltm_retrieval_settings(env) == (10, 0.3)
    (key,) = env
    assert key in caplog.text


def test_one_invalid_setting_keeps_the_other():
    env = {"LTM_TOP_K": "5", "LTM_RELEVANCE_SCORE": "high"}
    assert jwt_claims.ltm_retrieval_settings(env) == (5, 0.3)


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


_STUBBED_MODULES = (
    "ag_ui",
    "ag_ui.core",
    "ag_ui_strands",
    "bedrock_agentcore",
    "bedrock_agentcore.memory",
    "bedrock_agentcore.memory.integrations",
    "bedrock_agentcore.memory.integrations.strands",
    "bedrock_agentcore.memory.integrations.strands.config",
    "bedrock_agentcore.memory.integrations.strands.session_manager",
    "bedrock_agentcore.runtime",
    "shared.auth",
    "strands",
    "strands.models",
    "tools",
    "tools.code_interpreter",
    "tools.gateway",
)


def _load_agent(agent, monkeypatch):
    """Import an agent's real agent.py with its framework dependencies stubbed."""
    monkeypatch.syspath_prepend(str(ROOT / "agent-code"))
    for name in _STUBBED_MODULES:
        monkeypatch.setitem(sys.modules, name, _StubModule(name))
    sys.modules["ag_ui_strands"].StrandsAgent = type("StrandsAgent", (), {})
    spec = importlib.util.spec_from_file_location(
        f"_agent_{agent.replace('-', '_')}_under_test",
        ROOT / "agent-code" / agent / "agent.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_invalid_top_k_warns_and_uses_default(agent, monkeypatch, caplog):
    module = _load_agent(agent, monkeypatch)
    monkeypatch.setenv("LTM_TOP_K", "many")
    monkeypatch.delenv("LTM_RELEVANCE_SCORE", raising=False)
    monkeypatch.delenv("USE_LONG_TERM_MEMORY", raising=False)
    caplog.set_level(logging.WARNING)

    config = module._retrieval_config("sub-1", lambda **kw: kw)

    assert config == {
        "/users/sub-1/preferences": {"top_k": 10, "relevance_score": 0.3},
    }
    assert "LTM_TOP_K" in caplog.text


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_scopes_retrieval_to_verified_jwt_sub(agent):
    src = (ROOT / "agent-code" / agent / "agent.py").read_text()
    assert "user_id = extract_user_id_from_context(context)" in src
    assert "memory_retrieval_plan(user_id)" in src
    assert "retrieval_config=_retrieval_config(user_id" in src
    for forbidden in (
        'payload.get("user',
        'payload.get("actor',
        'payload["user',
        'payload["actor',
    ):
        assert forbidden not in src
