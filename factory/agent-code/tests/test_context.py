"""Context pack retrieval and the large-PR two-pass routing (no model, no network)."""

import subprocess

from factory import reviewer
from factory.context import build_pack
from factory.reviewer import Review


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_pack_picks_relevant_docs_from_base_commit_within_cap(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GUIDE.md").write_text(
        "# Memory\nThe memory_store keeps conversation turns per session.\n\n"
        "# Billing\nInvoices are monthly.\n"
        + "# Filler\n"
        + "unrelated words here\n" * 2000
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "docs" / "GUIDE.md").write_text(
        "# Later\nmemory_store fix documented after the PR\n"
    )
    _git(
        tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "later"
    )

    files = {
        "agent/memory_store.py": {
            "added": {1: "memory_store.put(session_id, turn)"},
            "removed": {},
            "hunks": [],
        }
    }
    pack, picked = build_pack(files, tmp_path, base, cap_tokens=3000)
    assert "docs/GUIDE.md::Memory" in picked
    assert "docs/GUIDE.md::Billing" not in picked
    assert "after the PR" not in pack  # docs come from the base commit, not a later one
    assert any(
        p.startswith("agentcore-reference::") for p in picked
    )  # shipped reference goes in whole
    assert len(pack) <= 3000 * 3.5 + 500


def _review(tmp_path, monkeypatch, answers):
    r = Review.__new__(Review)
    r.pr = {"number": 1, "title": "t", "body": ""}
    r.repo, r.models, r.calls = tmp_path, {}, []
    r.git = lambda *a, cwd=None: "\n".join(f"line{i}" for i in range(1, 400))

    def ask(model, system, prompt, max_tokens):
        r.calls.append(model)
        m = r.models.setdefault(
            model,
            {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0},
        )
        m["cache_write"] += len(prompt) // 3
        m["output_tokens"] += 300
        return answers[model]

    r.ask = ask
    monkeypatch.setattr(reviewer, "build_pack", lambda *a, **k: ("", []))
    return r


def _files(n):
    return {
        f"f{i}.py": {
            "added": {j: "x = 1" for j in range(1, 100)},
            "removed": {},
            "hunks": [(1, 99)],
        }
        for i in range(n)
    }


def test_large_pr_sol_on_all_then_astra_on_flagged_file(tmp_path, monkeypatch):
    sol = {
        "verdict": "changes needed",
        "summary": "s",
        "findings": [
            {"path": "f3.py", "line": 5, "severity": "major", "title": "a"},
            {"path": "f1.py", "line": 7, "severity": "nit", "title": "b"},
        ],
    }
    astra = {
        "verdict": "changes needed",
        "summary": "a",
        "findings": [
            {"path": "f3.py", "line": 6, "severity": "blocker", "title": "confirmed"},
        ],
    }
    r = _review(tmp_path, monkeypatch, {reviewer.SOL: sol, reviewer.ASTRA: astra})
    result, passes = r.two_pass(_files(5), "head", "base", "hdr", [], [])
    assert r.calls == [reviewer.SOL, reviewer.ASTRA]
    assert passes[1]["files"] == 1  # only the file Sol flagged above nit
    titles = {(f["path"], f["title"]) for f in result["findings"]}
    assert titles == {
        ("f3.py", "confirmed"),
        ("f1.py", "b"),
    }  # Astra replaces Sol on f3, Sol kept elsewhere
    assert r.cost() < reviewer.LARGE_REVIEW_USD


def test_large_pr_skips_astra_when_budget_is_spent(tmp_path, monkeypatch):
    sol = {
        "verdict": "approve-worthy",
        "summary": "s",
        "findings": [{"path": "f0.py", "line": 5, "severity": "major"}],
    }
    r = _review(tmp_path, monkeypatch, {reviewer.SOL: sol, reviewer.ASTRA: {}})
    # A cap the first pass fits inside, but which leaves too little for a useful second pass.
    monkeypatch.setattr(reviewer, "LARGE_REVIEW_USD", 0.02)
    result, passes = r.two_pass(_files(3), "head", "base", "hdr", ["f0.py"], [])
    assert r.calls == [reviewer.SOL]
    assert passes[0]["astra"].startswith("skipped")
    assert result["findings"] == sol["findings"]
    assert r.cost() <= 0.02  # the cap covers the whole review, not just the second pass


def test_first_pass_over_the_cap_is_refused_before_any_model_call(
    tmp_path, monkeypatch
):
    """The cap is a running budget: an unaffordable first pass is never paid for."""
    r = _review(tmp_path, monkeypatch, {reviewer.SOL: {}, reviewer.ASTRA: {}})
    monkeypatch.setattr(reviewer, "LARGE_REVIEW_USD", 0.001)
    try:
        r.two_pass(_files(3), "head", "base", "hdr", [], [])
    except reviewer.OverCap as e:
        assert e.projected > 0.001 and e.cap == 0.001
    else:
        raise AssertionError("an over-cap first pass must raise OverCap")
    assert r.calls == [] and r.cost() == 0.0
