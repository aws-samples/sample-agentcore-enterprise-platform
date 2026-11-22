"""A swallowed failure must reach the run row, and an outcome must not claim success for a partial result.

The digest was recording digest_written while its comment never reached the issue: _safe caught the
error, the caller stored it in a sub-field, and the field was not among the keys the digest records.
One test per layer.
"""

import pytest
from factory.ci_fixer import Run

from factory import ci_fixer, digest, github, server


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


def test_each_invocation_carries_only_its_own_errors(monkeypatch, tmp_path):
    """The reset is per invocation, in handle(), not per agent."""
    server._safe(boom("an earlier run in this session"))
    monkeypatch.setattr(server, "_dispatch", lambda role, p: {"outcome": "no_fix"})
    assert "swallowed_errors" not in server.handle({"role": "ci_fixer"})


def test_a_second_agent_in_one_run_does_not_erase_the_firsts_errors(tmp_path):
    """The merge handler refreshes the PM, so one run builds two agents."""
    server._safe(boom("the merge handler could not comment"))
    Run({"id": "first"}, tmp_path, tmp_path, None, "us-east-1")
    second = Run({"id": "second"}, tmp_path, tmp_path, None, "us-east-1")
    line = second.record({"outcome": "issues_dry_run"}, dry_run=True)
    assert line["swallowed_errors"] == [
        "RuntimeError: the merge handler could not comment"
    ]


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


# --- 4. every invocation leaves a row ----------------------------------------
# A handler that returns early, or raises before its own _finish, used to record nothing at all:
# no outcome to notice, nothing for the digest to count, no line in any cost figure.


def test_a_failing_comment_still_records_a_row(monkeypatch, tmp_path):
    """/pm: the answer cost tokens, so the row must survive a comment that 403s -- with its cost."""
    monkeypatch.setattr(
        github, "request", lambda *a, **k: {"number": 9, "user": {"login": "Robobc"}}
    )
    monkeypatch.setattr(server, "clone", lambda work: work)

    class FakeConsult:
        def __init__(self, *a):
            self.tracker = type(
                "T",
                (),
                {
                    "comment": lambda *a: boom(
                        "GitHub POST /issues/9/comments -> 403"
                    )(),
                    "ops": [],
                },
            )()
            self.AGENT, self.work = "product_manager", tmp_path

        def execute(self):
            return {"outcome": "consult_answered", "answer": "here is the answer"}

        def record(self, rec, dry_run=True):
            self.recorded = rec
            return {"run_id": "x", "outcome": rec["outcome"], "reason": rec["reason"]}

    monkeypatch.setattr(server, "Consult", FakeConsult)
    line = server._pm_comment(9, "a question", "ghs_x")
    assert line["run_id"] == "x"  # a row, where there used to be none
    assert line["outcome"] == "consult_not_posted"  # and it does not claim it posted
    assert "403" in line["reason"]


def test_a_handler_that_raises_before_finish_still_leaves_a_row(monkeypatch, tmp_path):
    monkeypatch.setattr(
        server, "_dispatch", lambda role, p: boom("clone failed: network")()
    )
    line = server.handle({"role": "developer"})
    assert line["run_id"] and line["agent"] == "developer"
    assert line["outcome"] == "failed" and "clone failed" in line["reason"]


def test_an_early_return_still_leaves_a_row_and_keeps_its_fields(monkeypatch, tmp_path):
    """The skips that returned a bare dict: no task, no write access, a refused author."""
    monkeypatch.setattr(
        server,
        "_dispatch",
        lambda role, p: {
            "outcome": "skipped",
            "reason": "author_not_allowed:someone",
            "decisions": [{"id": "P-abc123"}],
        },
    )
    line = server.handle({"role": "issue_command", "issue": {"number": 77}})
    assert line["run_id"] and line["outcome"] == "skipped"
    assert line["trigger"] == "77"  # the issue it was asked on
    assert line["decisions"] == [{"id": "P-abc123"}]  # the handler's own fields survive


def test_a_reply_with_no_outcome_is_recorded_as_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_dispatch", lambda role, p: {"issue": 121})
    assert server.handle({"role": "digest"})["outcome"] == "failed"


def test_a_handler_that_recorded_its_own_row_is_not_recorded_twice(monkeypatch):
    rows = []
    monkeypatch.setattr(
        server, "_dispatch", lambda role, p: rows.append(1) or {"run_id": "own"}
    )
    assert server.handle({"role": "pr_reviewer"})["run_id"] == "own"
    assert rows == [1]


def test_the_digest_counts_the_rows_this_net_creates():
    from factory import digest

    assert "issue_command" in digest.AGENTS
