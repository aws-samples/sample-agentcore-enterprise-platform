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
        "unit_costs": {"1d": digest.unit_costs({}), "7d": digest.unit_costs({})},
    }
    text = digest.render(summary, ["P-3fa91c"], [118])
    assert "Waiting on you" in text
    assert "P-3fa91c" in text and "#118" in text


def test_unit_cost_divides_spend_by_outcome_count_and_handles_zero():
    rows = {
        "pr_reviewer": [
            {"outcome": "review_posted", "est_cost_usd": 0.4},
            {"outcome": "reviewed_dry_run", "est_cost_usd": 0.2},
        ],
        "merge_handler": [{"outcome": "merged_recorded", "est_cost_usd": 0.0}],
        "developer": [{"outcome": "failed", "est_cost_usd": 1.0}],
    }
    unit = digest.unit_costs(rows)
    assert unit["per posted review"] == {"spend_usd": 0.6, "count": 1, "usd_each": 0.6}
    assert unit["per CI fix PR"]["usd_each"] is None  # no outcomes: no division by zero
    assert unit["per merged dev PR"] == {"spend_usd": 1.0, "count": 1, "usd_each": 1.0}


def test_approve_applies_close_relabel_and_create_including_old_proposals():
    t = issues.Tracker("o/r", "", publish=False)
    issues.apply_proposal(t, {"action": "close", "target": 7, "body": "done"})
    issues.apply_proposal(t, {"action": "needs_scoping", "target": 8, "body": "vague"})
    issues.apply_proposal(
        t, {"title": "T", "body": "b", "labels": ["factory:pm"]}
    )  # no action: create
    assert [(o["op"], o.get("issue")) for o in t.ops] == [
        ("comment", 7),
        ("close", 7),
        ("add_labels", 8),
        ("remove_label", 8),
        ("comment", 8),
        ("create", None),
    ]
