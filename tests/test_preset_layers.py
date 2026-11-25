"""Layered presets: journey x topology x posture compose to the flat presets."""

from __future__ import annotations

import itertools
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from infra_utils.platform_config import (
    JOURNEYS,
    POSTURES,
    SHORTCUTS,
    TOPOLOGIES,
    PlatformConfig,
    _merge_layers,
    compose_cli,
    compose_layers,
    refusal_reason,
    resolve_shortcut,
    validate_composed,
)

PRESETS = Path(__file__).resolve().parents[1] / "presets"

GUARDRAIL = "cannot attach the Bedrock Guardrail"
WORKSHOP = "refuses the guided workshop runner"

# Maintainer-reviewed table: every combination not listed is expected to pass.
EXPECTED_FAIL = {("multi-agent", t, "production"): GUARDRAIL for t in TOPOLOGIES}


@pytest.fixture
def tmp_path():
    # Repo-local scratch dir: the system temp dir may be unavailable.
    path = Path(tempfile.mkdtemp(prefix=".tmp-", dir=Path(__file__).resolve().parent))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _allow_placeholders(monkeypatch):
    # Layers carry the same sentinels as the shipped presets.
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")


def test_every_flat_preset_has_a_shortcut():
    assert set(SHORTCUTS) == {p.stem for p in PRESETS.glob("*.yaml")}


@pytest.mark.parametrize("name", sorted(SHORTCUTS))
def test_shortcut_is_equivalent_to_flat_preset(name):
    flat = yaml.safe_load((PRESETS / f"{name}.yaml").read_text())
    composed = validate_composed(resolve_shortcut(name))
    legacy = PlatformConfig.model_validate(flat)
    # Identical models imply identical expected_stacks().
    assert composed.model_dump() == legacy.model_dump()


@pytest.mark.parametrize(
    "journey,topology,posture",
    list(itertools.product(JOURNEYS, TOPOLOGIES, POSTURES)),
)
def test_combination_sweep(journey, topology, posture):
    reason = EXPECTED_FAIL.get((journey, topology, posture))
    if reason:
        with pytest.raises(ValueError, match=reason):
            validate_composed(compose_layers(journey, topology, posture))
    else:
        validate_composed(compose_layers(journey, topology, posture))


def test_posture_wins_for_security_and_journey_owns_agents():
    doc = compose_layers("migration", "centralized", "production")
    assert doc["security"] == POSTURES["production"]["security"]
    assert doc["agents"]["pattern"] == "langgraph-agent"
    assert doc["agents"]["model_id"] == POSTURES["production"]["agents"]["model_id"]


def test_merge_rules():
    doc = _merge_layers(
        [
            {"agents": {"pattern": "a", "memory": {"top_k": 1}}, "x": [1, 2]},
            {"agents": {"pattern": "b", "memory": {"top_k": 9, "long_term": True}}},
            {"x": [3], "security": {"networking": True}},
        ]
    )
    assert doc["agents"] == {"pattern": "a", "memory": {"top_k": 1, "long_term": True}}
    assert doc["x"] == [3]
    assert doc["security"] == {"networking": True}


def test_region_override():
    assert compose_layers("greenfield", "centralized", "dev", "eu-west-1")[
        "region"
    ] == ("eu-west-1")
    assert resolve_shortcut("greenfield", region="eu-west-1")["region"] == "eu-west-1"


def test_refuse_table():
    assert WORKSHOP in refusal_reason(posture="production", runner="workshop")
    assert refusal_reason(posture="dev", runner="workshop") is None
    with pytest.raises(ValueError, match=WORKSHOP):
        compose_layers("greenfield", "centralized", "production", runner="workshop")
    with pytest.raises(ValueError, match=WORKSHOP):
        resolve_shortcut("production", runner="workshop")
    resolve_shortcut("greenfield", runner="workshop")


@pytest.mark.parametrize(
    "args,kind",
    [
        (("nope", "centralized", "dev"), "journey"),
        (("greenfield", "nope", "dev"), "topology"),
        (("greenfield", "centralized", "nope"), "posture"),
    ],
)
def test_unknown_layer_is_named(args, kind):
    with pytest.raises(ValueError, match=f"unknown {kind} layer 'nope'"):
        compose_layers(*args)


def test_unknown_shortcut_is_named():
    with pytest.raises(ValueError, match="unknown shortcut 'nope'"):
        resolve_shortcut("nope")


def test_compose_cli_writes_manifest(tmp_path):
    out = tmp_path / "platform.yaml"
    assert compose_cli([str(out), "migration", "federated", "hardened", ""]) == 0
    text = out.read_text()
    assert text.startswith("# Generated from presets/")
    assert yaml.safe_load(text) == compose_layers("migration", "federated", "hardened")
    validate_composed(yaml.safe_load(text))


def test_compose_cli_refusal_writes_nothing(tmp_path, capsys):
    out = tmp_path / "platform.yaml"
    assert compose_cli([str(out), "multi-agent", "centralized", "production"]) == 1
    assert not out.exists()
    assert GUARDRAIL in capsys.readouterr().out
