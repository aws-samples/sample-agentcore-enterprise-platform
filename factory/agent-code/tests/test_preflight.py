"""Pre-flight: a role whose permission the installation lacks is skipped before any model call."""

import pytest
from factory.ci_fixer import Run

from factory import github, server

CREDS = {"app_id": "1", "installation_id": "2", "private_key_pem": "pem"}


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch, tmp_path):
    """No runs table, no clone, and a model call or a clone is a test failure."""
    monkeypatch.delenv("FACTORY_RUNS_TABLE", raising=False)
    monkeypatch.setattr(server, "Path", lambda *a: tmp_path)
    monkeypatch.setattr(
        server, "clone", lambda *a: pytest.fail("clone() ran after a pre-flight skip")
    )
    monkeypatch.setattr(
        Run, "chat", lambda *a, **k: pytest.fail("a model was called after a skip")
    )
    monkeypatch.setattr(github, "request", lambda *a, **k: {"full_name": server.REPO})


def grant(monkeypatch, missing: list[str]) -> None:
    monkeypatch.setattr(server, "load_app_creds", lambda: CREDS)
    monkeypatch.setattr(
        github, "installation_token", lambda *a: ("ghs_token", sorted(missing))
    )


def test_missing_permission_skips_before_any_model_call(monkeypatch):
    grant(monkeypatch, ["issues:write", "workflows:write"])
    rec = server.handle({"role": "developer"})
    assert rec["outcome"] == "skipped"
    assert rec["reason"] == "missing_permission:issues:write"
    assert rec["agent"] == "developer"
    assert rec["input_tokens_total"] == 0


def test_every_missing_permission_a_role_needs_is_named(monkeypatch):
    grant(monkeypatch, ["actions:read", "contents:write"])
    rec = server.handle({"role": "ci_fixer", "workflow_run": {"id": 7}})
    assert rec["reason"] == "missing_permission:actions:read,contents:write"
    assert (
        rec["trigger"] == "ci_fixer"
    )  # the run object is invalid; the role identifies it


def test_a_permission_another_role_needs_does_not_skip_this_one(monkeypatch):
    grant(monkeypatch, ["actions:read"])  # the reviewer never reads Actions
    assert server.preflight("pr_reviewer") is None


def test_an_unreachable_repo_skips_with_a_named_reason(monkeypatch):
    grant(monkeypatch, [])
    monkeypatch.setattr(
        github,
        "request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("GitHub GET /repos -> 404")),
    )
    skip = server.preflight("product_manager")
    assert skip["outcome"] == "skipped"
    assert skip["reason"].startswith("repo_unreachable: RuntimeError")


def test_a_dry_run_without_app_creds_is_not_skipped(monkeypatch):
    monkeypatch.setattr(server, "load_app_creds", lambda: None)
    assert server.preflight("developer") is None


def test_a_role_with_no_github_writes_is_never_skipped(monkeypatch):
    grant(monkeypatch, ["issues:write", "contents:write", "pull_requests:write"])
    assert server.preflight("digest") is None
    assert server.preflight("merge_handler") is None
