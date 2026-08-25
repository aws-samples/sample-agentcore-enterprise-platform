"""The deployment contract: expected_stacks() is the footprint everything trusts.

Pure-Python tests (no aws_cdk — the CI pytest job doesn't have it). The
synth-level half lives in scripts/check-contract.sh, which synthesizes every
preset and diffs the stack list against expected_stacks(); together they pin
app.py and the contract to each other.
"""

from pathlib import Path

import pytest

from infra_utils.platform_config import PlatformConfig, load_platform_config

REPO = Path(__file__).resolve().parents[1]
PRESETS = sorted((REPO / "presets").glob("*.yaml"))


def cfg(**overrides) -> PlatformConfig:
    return PlatformConfig.model_validate(overrides)


def suffixes(config: PlatformConfig, account: str = "") -> list[str]:
    prefix = f"{config.project}-{config.environment}-"
    return [s.removeprefix(prefix) for s in config.expected_stacks(account)]


def test_default_footprint():
    # Schema defaults: no networking/security, no A2A.
    assert suffixes(cfg()) == [
        "auth",
        "identity",
        "memory",
        "gateway",
        "runtime-orchestrator",
        "observability",
    ]


def test_everything_on():
    got = suffixes(
        cfg(
            agents={"a2a": True},
            security={"networking": True, "cloudtrail_alerting": True},
        )
    )
    assert got == [
        "networking",
        "security",
        "auth",
        "identity",
        "memory",
        "gateway",
        "runtime-orchestrator",
        "runtime-code-agent",
        "runtime-research-agent",
        "observability",
    ]


FEDERATED = {
    "deployment": {
        "strategy": "federated",
        "platform_account": "111111111111",
        "workload_accounts": ["222222222222"],
        "federation": {
            "gateway_url": "https://gw.example/mcp",
            "issuer_url": "https://issuer.example",
            "m2m_client_id": "client",
            "m2m_client_secret_name": "secret-name",
        },
    },
    "agents": {"a2a": True},
}


def test_federated_platform_side_runs_no_agents():
    got = suffixes(cfg(**FEDERATED), account="111111111111")
    assert "runtime-orchestrator" not in got
    assert "runtime-code-agent" not in got
    assert "memory" not in got  # memory is per-workload by design
    assert {"auth", "identity", "gateway", "observability"} <= set(got)


def test_federated_workload_side_has_no_auth_or_gateway():
    got = suffixes(cfg(**FEDERATED), account="222222222222")
    assert "auth" not in got
    assert "gateway" not in got
    assert {
        "identity",
        "memory",
        "runtime-orchestrator",
        "runtime-code-agent",
        "runtime-research-agent",
        "observability",
    } <= set(got)


def test_federated_unknown_account_is_a_hard_error():
    with pytest.raises(ValueError, match="neither"):
        cfg(**FEDERATED).expected_stacks("333333333333")


def test_stack_names_carry_project_and_environment():
    config = cfg(project="acme-platform", environment="prod")
    assert all(s.startswith("acme-platform-prod-") for s in config.expected_stacks())


def test_every_preset_produces_a_footprint():
    # Presets are fixtures for the whole pipeline: each must load and yield a
    # non-empty, duplicate-free stack list. The synth parity for these files
    # is scripts/check-contract.sh.
    assert PRESETS, "presets/ directory is empty?"
    for preset in PRESETS:
        config = load_platform_config(preset)
        stacks = config.expected_stacks()
        assert stacks, f"{preset.name} yields no stacks"
        assert len(stacks) == len(set(stacks)), f"{preset.name} has duplicates"
