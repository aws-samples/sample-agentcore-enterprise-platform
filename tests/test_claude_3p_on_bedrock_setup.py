"""claude-3p-on-bedrock setup wizard: the pure helpers that edit platform.yaml,
derive model ids and labels, and match certificates. No AWS calls."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
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
    r = subprocess.run(  # nosec B603 - runs this repo's script, fixed argv
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


class _NoSSM:
    def client(self, name):
        class C:
            def get_parameter(self, Name):
                raise RuntimeError("ParameterNotFound")

        return C()


def _agentcore_wizard(identity: dict, existing: dict | None = None):
    w = setup.Wizard.__new__(setup.Wizard)
    w.raw = {"project": "p", "environment": "e", "identity": identity}
    w.existing = existing or {}
    w.cfg = {"teams": [{"name": "engineering"}, {"name": "contractors"}]}
    w.aws = _NoSSM()
    w.ui = setup.UI(interactive=False, assume_yes=True)
    return w


def test_wizard_agentcore_defaults_to_the_platform_app():
    app = "2c3d4e5f-1111-2222-3333-444455556666"
    w = _agentcore_wizard({"idp": "entra_id", "mode": "direct", "client_id": app})
    w.step_agentcore()
    assert w.cfg["agentcore_tools"] == {"client_id": app}


def test_wizard_agentcore_keeps_existing_teams():
    app = "2c3d4e5f-1111-2222-3333-444455556666"
    w = _agentcore_wizard(
        {"idp": "entra_id", "mode": "direct", "client_id": app},
        {"agentcore_tools": {"client_id": app, "teams": ["engineering"]}},
    )
    w.step_agentcore()
    assert w.cfg["agentcore_tools"] == {"client_id": app, "teams": ["engineering"]}


def test_wizard_agentcore_without_direct_mode_needs_a_client_id():
    # Cognito platform: no app to default to, so non-interactive stops clearly.
    w = _agentcore_wizard({"idp": "cognito"})
    with pytest.raises(SystemExit, match="no answer"):
        w.step_agentcore()


# ── re-running the wizard must not lose what it doesn't ask about ────────────
def _wizard(existing: dict, answers: dict | None = None, *, yes: bool = True):
    w = setup.Wizard.__new__(setup.Wizard)
    w.raw = {"project": "p", "environment": "e"}
    w.existing = existing
    w.answers = answers or {}
    w.cfg = {
        "hostname": "claude.corp.example.com",
        "models": [{"id": "m1"}, {"id": "m2"}],
    }
    w.ui = setup.UI(interactive=False, assume_yes=yes)
    w._az_inspect = lambda *a: []
    return w


_TENANT = "9a8b7c6d-1111-2222-3333-444455556666"
_APP = "1f2e3d4c-1111-2222-3333-444455556666"


def test_rerun_keeps_sign_in_restrictions_it_does_not_ask_about():
    old_oidc = {
        "tenant_id": _TENANT,
        "client_id": _APP,
        "client_secret_name": "claude-3p/entra-oidc",
        "allowed_email_domains": ["corp.example.com"],
        "allowed_roles": ["Claude.Engineering"],
        "groups_claim": "groups",
        "extra_auth_params": {"domain_hint": "corp.example.com"},
    }
    w = _wizard(
        {"oidc": old_oidc},
        {
            "tenant_id": _TENANT,
            "client_id": _APP,
            "email_domains": ["corp.example.com"],
        },
    )
    w.step_entra()
    oidc = w.cfg["oidc"]
    assert oidc["allowed_roles"] == ["Claude.Engineering"]
    assert oidc["groups_claim"] == "groups"
    assert oidc["extra_auth_params"] == {"domain_hint": "corp.example.com"}


_OLD_TEAM = {
    "name": "engineering",
    "entra_roles": ["Claude.Engineering"],
    "models": ["m1"],
    "start_model": "m1",
    "settings": {"permissions": {"ask": ["Bash"]}},
    "desktop_settings": {"disabledBuiltinTools": ["WebFetch"]},
}


def test_rerun_keeps_team_and_default_policy_fields():
    w = _wizard(
        {
            "teams": [_OLD_TEAM],
            "default": {"models": ["m2"], "settings": {"model": "m2"}},
        }
    )
    w.step_teams()
    (team,) = w.cfg["teams"]
    assert team["start_model"] == "m1"
    assert team["settings"] == {"permissions": {"ask": ["Bash"]}}
    assert team["desktop_settings"] == {"disabledBuiltinTools": ["WebFetch"]}
    assert w.cfg["default"]["settings"] == {"model": "m2"}
    # The answers file keeps only what the wizard asked, no policy fields.
    assert "settings" not in w.answers["teams"][0]


def test_rerun_follows_a_renamed_team_by_its_roles():
    w = _wizard(
        {"teams": [_OLD_TEAM]},
        {"teams": [{**_OLD_TEAM, "name": "platform"}]},
    )
    w.step_teams()
    assert w.cfg["teams"][0]["name"] == "platform"
    assert w.cfg["teams"][0]["settings"] == _OLD_TEAM["settings"]


def test_explicit_empty_default_models_stay_empty():
    # `models: []` is a deny-all policy; accepting the defaults must not grant a model.
    w = _wizard({"teams": [_OLD_TEAM], "default": {"models": [], "deny_tools": []}})
    w.step_teams()
    assert w.cfg["default"]["models"] == []
    assert "deny_tools" not in w.cfg["default"]  # an explicit "none" stays none


def test_missing_default_models_get_the_suggestion():
    w = _wizard({"teams": [_OLD_TEAM]})
    w.step_teams()
    assert w.cfg["default"]["models"] == ["m2"]


def test_models_prompt_accepts_none():
    w = _wizard({"teams": [_OLD_TEAM]}, {"default": {"models": []}})
    w.step_teams()
    assert w.cfg["default"]["models"] == []


def test_removing_an_existing_team_needs_confirmation():
    other = {"name": "ops", "entra_roles": ["Claude.Ops"], "models": ["m2"]}
    answers = {"teams": [_OLD_TEAM]}  # one team, but two exist
    with pytest.raises(SystemExit, match="keep those teams"):
        _wizard({"teams": [_OLD_TEAM, other]}, answers, yes=False).step_teams()
    w = _wizard({"teams": [_OLD_TEAM, other]}, answers, yes=True)
    w.step_teams()
    assert [t["name"] for t in w.cfg["teams"]] == ["engineering"]


def test_replay_with_an_empty_optional_list():
    ui = setup.UI(interactive=False, assume_yes=False)
    assert ui.ask_list("denied tools", []) == []
    with pytest.raises(SystemExit, match="no answer"):
        ui.ask_list("domains", None)


def test_write_keeps_keys_the_wizard_does_not_own(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    monkeypatch.setattr(setup, "BACKUP_DIR", tmp_path / "bak")
    monkeypatch.setattr(setup, "ANSWERS_DIR", tmp_path / "answers")
    (tmp_path / "answers").mkdir()
    fixture = (
        Path(__file__).parent / "fixtures" / "contract" / "claude-3p-on-bedrock.yaml"
    )
    manifest = tmp_path / "platform.yaml"
    manifest.write_text(fixture.read_text())
    block = yaml.safe_load(manifest.read_text())["use_cases"]["claude-3p-on-bedrock"]
    block.update(desktop=False, session_ttl_hours=4, spend_admin_roles=["Admins"])
    w = setup.Wizard(manifest, setup.UI(False, True), {})
    w.existing = block
    w.cfg = {
        k: v
        for k, v in block.items()
        if k not in ("desktop", "session_ttl_hours", "spend_admin_roles")
    }
    w._claude_version = lambda: "2.1.311"
    w.step_write()
    written = yaml.safe_load(manifest.read_text())["use_cases"]["claude-3p-on-bedrock"]
    assert written["desktop"] is False
    assert written["session_ttl_hours"] == 4
    assert written["spend_admin_roles"] == ["Admins"]
