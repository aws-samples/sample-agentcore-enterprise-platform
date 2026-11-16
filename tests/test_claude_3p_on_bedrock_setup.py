"""claude-3p-on-bedrock setup wizard: the pure helpers that edit platform.yaml,
derive model ids and labels, and match certificates. No AWS calls."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
UC = REPO / "use-cases" / "claude-3p-on-bedrock"
spec = importlib.util.spec_from_file_location("c3p_setup", UC / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

GREENFIELD = (REPO / "presets" / "greenfield.yaml").read_text()
BLOCK = {"claude_version": "2.1.311", "hostname": "claude.corp.example.com"}


def test_model_ids_and_labels():
    cases = {
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0": (
            "claude-haiku-4-5",
            "Claude Haiku 4.5",
        ),
        "eu.anthropic.claude-opus-5": ("claude-opus-5", "Claude Opus 5"),
        "global.anthropic.claude-sonnet-5-5": (
            "claude-sonnet-5-5",
            "Claude Sonnet 5.5",
        ),
        "eu.anthropic.claude-opus-4-6-v1": ("claude-opus-4-6", "Claude Opus 4.6"),
        "global.anthropic.claude-sonnet-4-20250514-v1:0": (
            "claude-sonnet-4",
            "Claude Sonnet 4",
        ),
    }
    for profile, (mid, label) in cases.items():
        assert setup.model_id_from_profile(profile) == mid
        assert setup.model_label(mid) == label


def test_cert_coverage():
    assert setup.cert_covers("claude.corp.example.com", ["claude.corp.example.com"])
    assert setup.cert_covers("claude.corp.example.com", ["*.corp.example.com"])
    assert not setup.cert_covers(
        "claude.corp.example.com", ["*.example.com"]
    )  # one label only
    assert not setup.cert_covers("claude.corp.example.com", ["other.corp.example.com"])


def test_platform_vpc_overlap():
    assert setup.overlaps_platform_vpc(["10.0.5.0/24", "172.16.0.0/22"]) == [
        "10.0.5.0/24"
    ]
    assert setup.overlaps_platform_vpc(["172.16.0.0/22"]) == []


def test_best_public_zone_ignores_private_and_prefers_longest():
    zones = [
        {"Name": "example.com.", "Config": {"PrivateZone": False}},
        {"Name": "corp.example.com.", "Config": {"PrivateZone": False}},
        {"Name": "claude.corp.example.com.", "Config": {"PrivateZone": True}},
    ]
    assert (
        setup.best_public_zone("claude.corp.example.com", zones)["Name"]
        == "corp.example.com."
    )
    assert setup.best_public_zone("claude.other.org", zones) is None


def test_block_appended_and_comments_kept():
    out = setup.upsert_use_case_block(GREENFIELD, BLOCK)
    doc = yaml.safe_load(out)
    assert (
        doc["use_cases"]["claude-3p-on-bedrock"]["hostname"]
        == "claude.corp.example.com"
    )
    for (
        line
    ) in GREENFIELD.splitlines():  # every original line, comments included, survives
        assert line in out


def test_block_replaced_in_place_and_neighbours_kept():
    text = "project: p\nuse_cases:\n  other: {}\n  claude-3p-on-bedrock:\n    hostname: old.example.com\n    models: []\n\nobservability:\n  alarms: true\n"
    out = setup.upsert_use_case_block(text, BLOCK)
    doc = yaml.safe_load(out)
    assert doc["use_cases"]["other"] == {}
    assert doc["use_cases"]["claude-3p-on-bedrock"] == BLOCK
    assert doc["observability"] == {"alarms": True}
    assert "old.example.com" not in out


def test_inline_empty_use_cases():
    out = setup.upsert_use_case_block("project: p\nuse_cases: {}\n", BLOCK)
    assert yaml.safe_load(out)["use_cases"]["claude-3p-on-bedrock"] == BLOCK


def test_nested_scalar_edits():
    out = setup.set_nested_scalar(GREENFIELD, "security", "networking", "true", "VPC")
    assert yaml.safe_load(out)["security"] == {"networking": True}
    out = setup.set_nested_scalar(
        out, "agents", "model_id", "eu.anthropic.claude-sonnet-4-6"
    )
    agents = yaml.safe_load(out)["agents"]
    assert agents == {
        "pattern": "orchestrator",
        "model_id": "eu.anthropic.claude-sonnet-4-6",
    }
    out = setup.set_nested_scalar(
        out, "agents", "model_id", "eu.anthropic.claude-opus-5"
    )
    assert (
        yaml.safe_load(out)["agents"]["model_id"] == "eu.anthropic.claude-opus-5"
    )  # replaced, not duplicated
    out = setup.set_nested_scalar(
        out, "deployment", "platform_account", '"111111111111"'
    )
    assert yaml.safe_load(out)["deployment"]["platform_account"] == "111111111111"
    assert setup.set_nested_scalar(out, "nosuchkey", "x", "1") is None


def test_top_scalar_edit():
    out = setup.set_top_scalar(GREENFIELD, "region", "eu-west-1")
    assert yaml.safe_load(out)["region"] == "eu-west-1"


def test_check_fails_cleanly_without_the_block(tmp_path):
    m = tmp_path / "platform.yaml"
    m.write_text(GREENFIELD)
    r = subprocess.run(  # nosec B603
        [sys.executable, str(UC / "setup.py"), "check", "--manifest", str(m)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r.returncode == 1
    assert "use_cases.claude-3p-on-bedrock missing" in r.stdout
    assert "Traceback" not in r.stderr


def test_public_certificate_only_for_public_names():
    # Found in the wizard test: a public ACM request for a .internal name stays pending forever.
    assert setup.is_public_name("claude.corp.example.com")
    for internal in (
        "claude.example.internal",
        "claude.local",
        "gw.corp",
        "x.home.arpa",
    ):
        assert not setup.is_public_name(internal)


def test_region_geo_prefix():
    # The suggested platform model should keep inference in the deployment's geography.
    assert setup.region_geo_prefix("eu-west-1") == "eu"
    assert setup.region_geo_prefix("us-east-1") == "us"
    assert setup.region_geo_prefix("ap-southeast-2") == "apac"


def test_role_values_checked_at_entry():
    # Found in the wizard test: pasted text was accepted, then offered back as the default.
    assert setup.valid_role("Claude.Engineering")
    assert not setup.valid_role("=== 6. Teams (Entra App Role -> models) ===")
    assert not setup.valid_role("  ")
    assert setup.roles_ok(["Claude.Engineering"]) is None
    assert "no spaces" in setup.roles_ok(["Claude Engineering"])
    assert setup.roles_ok([]) == "at least one role"


def test_validation_errors_are_readable():
    import pytest

    gc = setup.gc
    with pytest.raises(ValueError) as exc:
        gc.load({"claude_version": "nope"})
    lines = setup.readable_errors(exc.value)
    assert any(line.startswith("claude_version:") for line in lines)
    assert not any("Traceback" in line for line in lines)


def test_answers_file_keeps_no_customer_identifiers(tmp_path, monkeypatch):
    # The answers file must never carry tenant/app ids, domains, hostnames or networks.
    monkeypatch.setattr(setup, "ANSWERS_DIR", tmp_path / "answers")
    w = setup.Wizard.__new__(setup.Wizard)
    w.raw = {"project": "p", "environment": "e"}
    w.answers = {
        "region": "eu-west-1",
        "models": ["eu.anthropic.claude-opus-5"],
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "client_id": "11111111-1111-1111-1111-111111111111",
        "email_domains": ["corp.example.com"],
        "hostname": "claude.corp.example.com",
        "client_cidrs": ["172.16.0.0/16"],
    }
    (tmp_path / "answers").mkdir()
    w._save_answers()
    f = tmp_path / "answers" / "p-e.answers.yaml"
    saved = yaml.safe_load(f.read_text())
    assert set(saved) == {"region", "models"}
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_backups_go_outside_the_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ANSWERS_DIR", tmp_path / "a")
    monkeypatch.setattr(setup, "BACKUP_DIR", tmp_path / "a" / "backups")
    b = setup.backup_path(REPO / "platform.yaml")
    assert REPO not in b.parents
    assert b.name.startswith("platform.yaml.")
