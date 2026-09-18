"""design_plan(): the Design-phase read-back is pure, complete and honest."""

from __future__ import annotations

from pathlib import Path

from infra_utils.platform_config import (
    PlatformConfig,
    design_plan,
    load_platform_config,
)

PRESETS = Path(__file__).resolve().parents[1] / "presets"


def text(config: PlatformConfig, account: str = "") -> str:
    return "\n".join(design_plan(config, account))


def test_greenfield_plan_names_the_footprint_and_deploys_nothing():
    t = text(load_platform_config(PRESETS / "greenfield.yaml"))
    assert "Topology: centralized" in t
    assert "Sign-in: cognito" in t
    assert "Stacks (6):" in t
    assert "agentcore-workshop-dev-gateway" in t
    assert "Controls on: none" in t
    assert "Use cases: none yet" in t
    assert "Nothing has been deployed" in t


def test_controls_and_use_cases_show_up():
    c = PlatformConfig(
        project="proj",
        security={"networking": True, "require_guardrails": True},
        observability={"alarms": True, "alarm_email": "ops@corp.internal"},
        use_cases={"hello-platform": {}},
    )
    t = text(c)
    assert "private networking (VPC mode)" in t
    assert "guardrail-enforced inference" in t
    assert "alarms + dashboard" in t
    assert "hello-platform  (uc-hello-platform)" in t
    assert "proj-dev-networking" in t


def test_federated_plan_is_per_side(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    c = load_platform_config(PRESETS / "federated.yaml")
    platform = text(c, "000000000000")
    workload = text(c, "123456789012")
    assert "this account is the platform side" in platform and "-auth" in platform
    assert (
        "this account is the workload side" in workload
        and "runtime-orchestrator" in workload
    )
    assert "-auth" not in workload
    # Unlisted account: say so, do not invent a footprint.
    assert "deploy from a listed account" in text(c, "999999999999")


def test_migration_plan_is_embedded(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    t = text(load_platform_config(PRESETS / "migration.yaml"))
    assert "Adapter mapping" in t and "POST /invocations" in t
    assert "Warnings:" in t  # the placeholders, surfaced not hidden


def test_plan_is_plain_text():
    for line in design_plan(load_platform_config(PRESETS / "greenfield.yaml")):
        assert "\x1b" not in line and "\t" not in line
