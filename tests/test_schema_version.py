"""schema_version in platform.yaml: legacy files, rejections, migrations."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from infra_utils import platform_config as pc

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "upgrade" / "v0.1.0-platform.yaml"
MIGRATION_MSG = "Add `schema_version: 1`"


def _fixture() -> dict:
    return yaml.safe_load(FIXTURE.read_text())


def test_fixture_is_a_legacy_file():
    assert "schema_version" not in _fixture()


def test_legacy_fixture_loads_as_v1_with_migration_message(capsys):
    cfg = pc.PlatformConfig.model_validate(_fixture())
    assert cfg.schema_version == 1
    assert MIGRATION_MSG in capsys.readouterr().err


def test_explicit_version_1_loads_silently(capsys):
    doc = _fixture()
    doc["schema_version"] = 1
    cfg = pc.validate_composed(doc)
    assert cfg.schema_version == 1
    assert "schema_version" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (pc.CURRENT_SCHEMA_VERSION + 1, "upgrade the accelerator"),
        ("1", "must be an integer"),
        (True, "must be an integer"),
        (0, "must be 1 or higher"),
    ],
)
def test_invalid_versions_rejected(value, message):
    doc = _fixture()
    doc["schema_version"] = value
    with pytest.raises(ValidationError, match=message):
        pc.PlatformConfig.model_validate(doc)


def _v1_to_v2(doc: dict) -> dict:
    out = dict(doc)
    out["project"] = out.pop("legacy_project")
    return out


def test_migration_chain_runs_before_validation(monkeypatch, capsys):
    monkeypatch.setattr(pc, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(pc, "MIGRATIONS", {1: _v1_to_v2})
    doc = _fixture()
    doc["schema_version"] = 1
    doc["legacy_project"] = doc.pop("project").replace("workshop", "renamed")
    cfg = pc.PlatformConfig.model_validate(doc)
    assert cfg.schema_version == 2
    assert cfg.project == "agentcore-renamed"
    assert "migrated to 2" in capsys.readouterr().err


def test_legacy_file_is_migrated_too(monkeypatch):
    monkeypatch.setattr(pc, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(pc, "MIGRATIONS", {1: lambda d: dict(d)})
    assert pc.PlatformConfig.model_validate(_fixture()).schema_version == 2


def test_missing_migration_step_is_an_error(monkeypatch):
    monkeypatch.setattr(pc, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(pc, "MIGRATIONS", {})
    doc = _fixture()
    doc["schema_version"] = 1
    with pytest.raises(ValidationError, match="no migration registered"):
        pc.PlatformConfig.model_validate(doc)


def test_every_preset_and_shortcut_carries_version_1():
    for path in sorted((ROOT / "presets").glob("*.yaml")):
        assert yaml.safe_load(path.read_text())["schema_version"] == 1, path
    for name in pc.SHORTCUTS:
        assert pc.resolve_shortcut(name)["schema_version"] == 1, name
