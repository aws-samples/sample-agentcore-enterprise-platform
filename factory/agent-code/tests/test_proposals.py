"""Proposals: a PM proposal is a digest-issue comment that /approve or /reject resolves (no AWS, no GitHub)."""

from factory import digest, issues


def test_proposal_round_trips_and_a_decision_removes_it(monkeypatch):
    sent = {}
    monkeypatch.setattr(issues, "digest_issue", lambda repo, token, create: (5, False))
    tracker = issues.Tracker("o/r", "", publish=True)
    tracker._do = lambda op, method, path, body=None: sent.update(body=body) or {}
    pid = tracker.propose("Add <retry> to the reviewer", "why", ["factory:pm"])
    comments = [{"body": sent["body"]["body"]}]
    # The JSON in the marker must not contain a raw '>' that would close the HTML comment early.
    assert "<retry>" not in comments[0]["body"].split("factory:proposal", 1)[1]
    monkeypatch.setattr(issues, "_comments", lambda repo, token, number: comments)
    assert list(issues.pending_proposals("o/r", "", 5)) == [pid]
    comments.append({"body": f"<!-- factory:resolved {pid} approved -->"})
    assert issues.pending_proposals("o/r", "", 5) == {}


def test_digest_lists_what_waits_on_a_human():
    summary = {
        "day": "2026-11-17",
        "runs": 2,
        "cost_usd_total": 0.5,
        "cost_usd_by_role": {"pr_reviewer": 0.5},
        "outcomes_by_role": {"pr_reviewer": {"reviewed": 2}},
        "prs_opened": [],
        "prs_merged": [],
        "failures": [],
    }
    text = digest.render(summary, ["P-3fa91c"], [118])
    assert "Waiting on you" in text
    assert "P-3fa91c" in text and "#118" in text
