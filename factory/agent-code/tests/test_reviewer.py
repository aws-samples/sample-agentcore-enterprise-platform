"""Diff parsing and review payload anchoring (no model, no network)."""

from factory.reviewer import build_review, parse_diff, render

DIFF = """diff --git a/app.py b/app.py
index 1..2 100644
--- a/app.py
+++ b/app.py
@@ -2,0 +3,2 @@ x
+import os
+print(os.environ)
@@ -9 +10,0 @@ y
-removed = True
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def test_parse_diff_tracks_new_line_numbers_and_deletions():
    files = parse_diff(DIFF)
    assert list(files) == ["app.py"]
    assert files["app.py"]["added"] == {3: "import os", 4: "print(os.environ)"}
    assert files["app.py"]["removed"] == {11: ["removed = True"]}
    text = render(
        "app.py",
        "\n".join(f"line{i}" for i in range(1, 13)),
        files["app.py"],
        context=1,
    )
    assert "    3+| line3" in text and "     -| removed = True" in text


def test_only_added_lines_become_inline_comments():
    files = parse_diff(DIFF)
    result = {
        "summary": "s",
        "verdict": "changes needed",
        "findings": [
            {
                "path": "app.py",
                "line": 4,
                "severity": "major",
                "title": "leaks env",
                "body": "b",
                "suggestion": "",
            },
            {
                "path": "app.py",
                "line": 1,
                "severity": "minor",
                "title": "unchanged line",
                "body": "b",
            },
            {
                "path": "app.py",
                "line": 3,
                "severity": "nit",
                "title": "n1",
                "body": "b",
            },
            {
                "path": "app.py",
                "line": 3,
                "severity": "nit",
                "title": "n2",
                "body": "b",
            },
            {
                "path": "app.py",
                "line": 3,
                "severity": "nit",
                "title": "n3",
                "body": "b",
            },
            {
                "path": "app.py",
                "line": 3,
                "severity": "bogus",
                "title": "dropped",
                "body": "b",
            },
        ],
    }
    review = build_review(result, files, "f" * 40, "m", [])
    assert review["event"] == "REQUEST_CHANGES" and review["commit_id"] == "f" * 40
    assert [(c["line"], c["body"].split("\n")[0]) for c in review["comments"]] == [
        (4, "**major: leaks env**"),
        (3, "**nit: n1**"),
        (3, "**nit: n2**"),
    ]
    assert "`app.py:1`" in review["body"] and "dropped" not in review["body"]
