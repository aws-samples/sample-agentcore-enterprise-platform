"""Per-role config: the live rows, the env override, and the fail-closed default."""

import pytest
from factory.ci_fixer import Run

from factory import config

ROLES = ("ci_fixer", "pr_reviewer", "developer", "product_manager")


@pytest.fixture(autouse=True)
def clean_errors(monkeypatch):
    monkeypatch.setattr(config, "ERRORS", [])
    for role in (*ROLES, "merge_handler", "digest", "nobody"):
        for field in ("token_cap", "triage_model", "fix_model"):
            monkeypatch.delenv(f"FACTORY_{role.upper()}_{field.upper()}", raising=False)


# --- fail closed --------------------------------------------------------------


def test_a_role_with_no_row_cannot_publish_and_says_why(monkeypatch):
    monkeypatch.setenv("FACTORY_PUBLISH", "true")
    assert config.publish("nobody") is False
    assert config.role("nobody") == {
        "models": {},
        "token_cap": 0,
        "cost_cap_usd": 0.0,
        "publish_env": "",
    }
    assert any("no config row for" in e and "nobody" in e for e in config.ERRORS)


def test_an_unknown_repo_cannot_publish_any_role(monkeypatch):
    """A second repo is config, not code: until its block exists, every role fails closed."""
    for switch in ("FACTORY_PUBLISH", "FACTORY_REVIEW_PUBLISH", "FACTORY_DEV_PUBLISH"):
        monkeypatch.setenv(switch, "true")
    for role in ROLES:
        assert config.publish(role, repo="someone/other-repo") is False
    assert config.ERRORS


def test_a_role_with_no_switch_never_publishes(monkeypatch):
    monkeypatch.setenv("FACTORY_PUBLISH", "true")
    assert config.publish("merge_handler") is False
    assert config.publish("digest") is False
    assert config.ERRORS == []  # they have rows; they simply name no switch


def test_a_missing_model_is_refused_before_bedrock_is_called(monkeypatch, tmp_path):
    """Fail closed end to end: no row -> no model id -> the call is refused, not sent."""
    run = Run({"id": "1"}, tmp_path, tmp_path, None, "us-east-1")
    monkeypatch.setattr(
        run,
        "bedrock",
        type("Boom", (), {"converse": lambda *a, **k: pytest.fail("called Bedrock")})(),
    )
    with pytest.raises(ValueError, match="no model configured"):
        run.chat(config.models("nobody").get("fix", ""), [], [], 10)


# --- the switch still comes from the environment ------------------------------


def test_publish_is_off_unless_the_rows_own_switch_says_true(monkeypatch):
    monkeypatch.delenv("FACTORY_REVIEW_PUBLISH", raising=False)
    assert config.publish("pr_reviewer") is False
    monkeypatch.setenv("FACTORY_REVIEW_PUBLISH", "yes")  # only "true" counts
    assert config.publish("pr_reviewer") is False
    monkeypatch.setenv("FACTORY_REVIEW_PUBLISH", "true")
    assert config.publish("pr_reviewer") is True
    monkeypatch.setenv("FACTORY_PUBLISH", "true")  # another role's switch is not ours
    monkeypatch.setenv("FACTORY_REVIEW_PUBLISH", "false")
    assert config.publish("pr_reviewer") is False


def test_a_field_can_be_overridden_at_deploy_time(monkeypatch):
    monkeypatch.setenv("FACTORY_CI_FIXER_FIX_MODEL", "us.anthropic.claude-opus-5-5")
    monkeypatch.setenv("FACTORY_CI_FIXER_TOKEN_CAP", "50000")
    row = config.role("ci_fixer")
    assert row["models"]["fix"] == "us.anthropic.claude-opus-5-5"
    assert row["token_cap"] == 50_000
    assert config.ERRORS == []


def test_a_non_numeric_override_keeps_the_configured_cap_and_records_it(monkeypatch):
    monkeypatch.setenv("FACTORY_CI_FIXER_TOKEN_CAP", "lots")
    assert config.role("ci_fixer")["token_cap"] == 200_000
    assert any("is not a number" in e for e in config.ERRORS)


# --- the rows are the single source of truth ----------------------------------


def test_every_role_module_reads_its_models_from_the_config(monkeypatch):
    from factory import ci_fixer, developer, pm, reviewer

    rows = config.ROLES[config.REPO]
    assert (ci_fixer.TRIAGE_MODEL, ci_fixer.FIX_MODEL) == (
        rows["ci_fixer"]["models"]["triage"],
        rows["ci_fixer"]["models"]["fix"],
    )
    assert (reviewer.SOL, reviewer.ASTRA) == (
        rows["pr_reviewer"]["models"]["first_pass"],
        rows["pr_reviewer"]["models"]["deep_pass"],
    )
    assert (developer.PLAN_MODEL, developer.CODE_MODEL) == (
        rows["developer"]["models"]["plan"],
        rows["developer"]["models"]["code"],
    )
    assert (pm.PM_MODEL, pm.BACKLOG_MODEL) == (
        rows["product_manager"]["models"]["main"],
        rows["product_manager"]["models"]["backlog"],
    )
    assert reviewer.LARGE_REVIEW_USD == rows["pr_reviewer"]["cost_cap_usd"]
    assert reviewer.Review.TOKEN_CAP == rows["pr_reviewer"]["token_cap"]
    assert developer.Developer.TOKEN_CAP == rows["developer"]["token_cap"]
    assert pm.ProductManager.TOKEN_CAP == rows["product_manager"]["token_cap"]
    assert ci_fixer.Run.TOKEN_CAP == rows["ci_fixer"]["token_cap"]


def test_the_reviewer_is_not_from_the_coders_model_family():
    rows = config.ROLES[config.REPO]
    coders = set(rows["developer"]["models"].values()) | set(
        rows["ci_fixer"]["models"].values()
    )
    assert not coders & set(rows["pr_reviewer"]["models"].values())


def test_a_config_error_rides_on_the_run_row(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ERRORS", ["no config row for (x, y): publish off"])
    run = Run({"id": "1"}, tmp_path, tmp_path, None, "us-east-1")
    line = run.record({"outcome": "skipped"}, dry_run=True)
    assert line["config_errors"] == ["no config row for (x, y): publish off"]
