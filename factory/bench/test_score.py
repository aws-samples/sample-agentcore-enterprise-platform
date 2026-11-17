"""Benchmark scoring rules. Run: python -m pytest factory/bench -q"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_bench import score, totals

BUG = [{"path": "a.py", "line": 10, "categories": ["security"], "bug": "secret logged"}]


def f(path="a.py", line=10, sev="major", cat="security", title="t"):
    return {
        "path": path,
        "line": line,
        "severity": sev,
        "category": cat,
        "title": title,
    }


def test_hit_needs_file_line_window_and_category():
    assert score(BUG, [f(line=15)])["tp"] == 1
    assert score(BUG, [f(line=16)]) == {
        "tp": 0,
        "fn": 1,
        "fp": 1,
        "missed": ["secret logged"],
        "false_positives": ["a.py:16 t"],
    }
    assert score(BUG, [f(cat="correctness")])["tp"] == 0
    assert score(BUG, [f(path="b.py")])["tp"] == 0
    assert score(BUG, [f(sev="nit")]) == {
        "tp": 0,
        "fn": 1,
        "fp": 0,
        "missed": ["secret logged"],
        "false_positives": [],
    }


def test_only_extra_blockers_and_majors_count_as_false_positives():
    s = score([], [f(sev="blocker"), f(sev="minor"), f(sev="nit")])
    assert (s["tp"], s["fp"], s["fn"]) == (0, 1, 0)


def test_one_finding_cannot_catch_two_bugs():
    two = BUG + [
        {"path": "a.py", "line": 12, "categories": ["security"], "bug": "second"}
    ]
    assert score(two, [f()])["tp"] == 1


def test_totals():
    t = totals([{"tp": 3, "fp": 1, "fn": 1}, {"tp": 1, "fp": 1, "fn": 0}])
    assert t == {
        "tp": 4,
        "fp": 2,
        "fn": 1,
        "precision": 0.667,
        "recall": 0.8,
        "f1": 0.727,
    }


def test_a_split_bug_can_be_reported_at_any_listed_location():
    split = [
        {
            "path": "a.py",
            "line": 10,
            "categories": ["security"],
            "bug": "split",
            "alt": [{"path": "b.py", "line": 40}],
        }
    ]
    assert score(split, [f(path="b.py", line=43)])["tp"] == 1
    assert score(split, [f(path="b.py", line=10)])["tp"] == 0
