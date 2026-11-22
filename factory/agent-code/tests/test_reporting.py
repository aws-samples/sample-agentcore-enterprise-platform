"""A swallowed failure must reach the run row, and an outcome must not claim success for a partial result.

The digest was recording digest_written while its comment never reached the issue: _safe caught the
error, the caller stored it in a sub-field, and the field was not among the keys the digest records.
One test per layer.
"""

import pytest
from factory.ci_fixer import Run

from factory import ci_fixer, digest, server


@pytest.fixture(autouse=True)
def no_table(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_RUNS_TABLE", raising=False)
    monkeypatch.setattr(server, "Path", lambda *a: tmp_path)
    monkeypatch.setattr(server, "preflight", lambda role: None)
    ci_fixer.SWALLOWED.clear()


def boom(message: str):
    def fn():
        raise RuntimeError(message)

    return fn


# --- 1. _safe must not erase evidence, at every call site ---------------------


def test_safe_records_what_it_swallows(tmp_path):
    rec = server._safe(boom("GitHub POST /issues/121/comments -> 403"))
    assert rec["outcome"] == "failed"
    assert ci_fixer.SWALLOWED == [
        "RuntimeError: GitHub POST /issues/121/comments -> 403"
    ]


def test_an_error_stored_in_a_sub_field_still_reaches_the_row(tmp_path):
    """The digest's shape: the failure goes into a sub-field, so only the row can carry it."""
    run = Run({"id": "1"}, tmp_path, tmp_path, None, "us-east-1")
    rec = {"outcome": "digest_written"}
    rec["digest_post"] = server._safe(boom("no Issues permission"))
    line = run.record(rec, dry_run=False)
    assert line["swallowed_errors"] == ["RuntimeError: no Issues permission"]


def test_the_error_that_became_the_outcome_is_not_repeated(tmp_path):
    run = Run({"id": "1"}, tmp_path, tmp_path, None, "us-east-1")
    line = run.record(server._safe(boom("the whole run failed")), dry_run=False)
    assert line["reason"] == "RuntimeError: the whole run failed"
    assert "swallowed_errors" not in line


def test_each_run_carries_only_its_own_errors(tmp_path):
    server._safe(boom("an earlier run in this session"))
    run = Run({"id": "2"}, tmp_path, tmp_path, None, "us-east-1")  # resets the list
    assert "swallowed_errors" not in run.record({"outcome": "no_fix"}, dry_run=True)


# --- 2. the outcome tells the truth ------------------------------------------


def _digest(monkeypatch, tmp_path, post):
    monkeypatch.setattr(
        digest.Digest,
        "execute",
        lambda self: {"outcome": "digest_written", "summary": {"day": "2026-11-21"}},
    )
    monkeypatch.setattr(server, "_post_digest", post)
    return server.handle({"role": "digest"})


def test_a_digest_that_could_not_post_does_not_claim_it_was_written(
    monkeypatch, tmp_path
):
    line = _digest(monkeypatch, tmp_path, lambda s: boom("no Issues permission")())
    assert line["outcome"] == "digest_not_posted"
    assert "not posted" in line["reason"] and "no Issues permission" in line["reason"]


def test_a_digest_that_posted_keeps_its_success_outcome(monkeypatch, tmp_path):
    line = _digest(monkeypatch, tmp_path, lambda s: {"issue": 121, "pin": True})
    assert line["outcome"] == "digest_written"
    assert "swallowed_errors" not in line


# --- 3. the evidence survives in the recorded keys ---------------------------


def test_the_digest_records_whether_the_post_landed(monkeypatch, tmp_path):
    assert "digest_post" in digest.Digest.EXTRA_KEYS
    agent = digest.Digest(tmp_path, "us-east-1", server.REPO, "2026-11-21")
    rec = {
        "outcome": "digest_not_posted",
        "digest_post": {"outcome": "failed", "reason": "no Issues permission"},
    }
    extra = {k: ci_fixer.runs_value(rec[k]) for k in agent.EXTRA_KEYS if rec.get(k)}
    assert "no Issues permission" in extra["digest_post"]
