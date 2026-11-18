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


def test_doc_only_diffs_use_sol_even_when_they_talk_about_security():
    from factory.reviewer import ASTRA, SOL, choose_model

    docs = {
        "docs/SECURITY_CONTROLS.md": {
            "added": {1: "IAM PolicyStatement with secretsmanager"},
            "removed": {},
            "hunks": [(1, 1)],
        },
        "README.md": {"added": {2: "x"}, "removed": {}, "hunks": [(2, 1)]},
    }
    assert choose_model(docs, 900)[0] == SOL
    code = dict(
        docs,
        **{
            "stacks/x.py": {
                "added": {1: "iam.PolicyStatement()"},
                "removed": {},
                "hunks": [(1, 1)],
            }
        },
    )
    model, _reason, sensitive = choose_model(code, 10)
    assert model == ASTRA and "stacks/x.py" in sensitive
    assert (
        choose_model(
            {"app.py": {"added": {1: "x = 1"}, "removed": {}, "hunks": [(1, 1)]}}, 10
        )[0]
        == SOL
    )


def test_widening_an_iam_resource_list_goes_to_astra():
    from factory.reviewer import ASTRA, choose_model

    files = {
        "stacks/runtime_stack.py": {
            "added": {643: '                resources=["*"],'},
            "removed": {},
            "hunks": [(643, 1)],
        }
    }
    assert choose_model(files, 2)[0] == ASTRA


def _files(**by_path):
    return {
        p: {
            "added": dict(enumerate(lines, 1)),
            "removed": {},
            "hunks": [(1, len(lines))],
        }
        for p, lines in by_path.items()
    }


def test_astra_only_for_the_named_risk_areas():
    from factory.reviewer import ASTRA, SOL, choose_model

    sol = [
        # a bare word is not a risk area
        {
            "migration/adapter/adapter.py": [
                '"""Resolve each declared secret by name."""',
                "names = settings.secret_names",
            ]
        },
        # Secrets Manager fakes and ARN strings in tests
        {
            "tests/test_adapter.py": [
                "def get_secret_value(self, SecretId):",
                '"arn:aws:secretsmanager:us-east-1:1:secret:x"',
            ]
        },
        {"scripts/usecase.py": ["print('rotate your secrets regularly')"]},
    ]
    astra = [
        {"stacks/runtime_stack.py": ['resources=["*"],']},  # IAM resource list
        {
            "app/handler.py": ['client = boto3.client("secretsmanager")']
        },  # Secrets Manager API
        {"app/handler.py": ["value = sm.get_secret_value(SecretId=name)"]},
        {"app/crypto.py": ["key = aws_kms.Key(self, 'K')"]},  # KMS resource
        {"agent-code/shared/auth.py": ["x = 1"]},  # auth handling code, by path
        {
            "app/client.py": ['headers = {"Authorization": f"Bearer {t}"}']
        },  # token handling
        {".github/workflows/ci.yml": ["run: echo hi"]},
        {"stacks/networking_stack.py": ["x = 1"]},
        {
            "stacks/app.py": ["sg.add_ingress_rule(aws_ec2.Peer.any_ipv4(), port)"]
        },  # network config
    ]
    for case in sol:
        assert choose_model(_files(**case), 20)[0] == SOL, case
    for case in astra:
        assert choose_model(_files(**case), 20)[0] == ASTRA, case
    assert (
        choose_model(_files(**{"app/big.py": ["x = 1"]}), 801)[0] == ASTRA
    )  # size alone


def test_publish_payload_is_validated():
    import pytest

    from factory.server import validate

    ok = {
        "role": "pr_reviewer",
        "pull_request": {"number": 117},
        "publish": True,
        "from_run_sk": "2026-11-17T17:00:00.000000+00:00#f6d11406eedb",
        "extra_findings": [
            {"path": "a.py", "line": 3, "severity": "major", "title": "t", "body": "b"}
        ],
    }
    assert validate(ok) == {"id": 117}
    for bad in (
        {**ok, "from_run_sk": "x"},
        {
            **ok,
            "extra_findings": [
                {
                    "path": "a.py",
                    "line": "3",
                    "severity": "major",
                    "title": "t",
                    "body": "b",
                }
            ],
        },
        {
            **ok,
            "extra_findings": [
                {
                    "path": "a.py",
                    "line": 3,
                    "severity": "approve",
                    "title": "t",
                    "body": "b",
                }
            ],
        },
    ):
        with pytest.raises(ValueError):
            validate(bad)


def test_review_posting_needs_global_switch_or_explicit_publish(monkeypatch, tmp_path):
    """FACTORY_REVIEW_PUBLISH=false: a normal review never posts; only publish: true posts (one-off)."""
    from factory import server

    posted, published = [], []
    pr = {
        "number": 7,
        "user": {"login": "someone"},
        "base": {"repo": {"full_name": server.REPO}},
    }
    monkeypatch.setattr(server, "REVIEW_PUBLISH", False)
    monkeypatch.setattr(server, "load_app_creds", lambda: {"app_id": "1"})
    monkeypatch.setattr(
        server.github, "installation_token", lambda creds, repo: ("tok", [])
    )
    monkeypatch.setattr(server.github, "get_pull", lambda repo, n, token: pr)
    monkeypatch.setattr(
        server.github, "post_review", lambda *a: posted.append(a) or "url"
    )
    monkeypatch.setattr(server, "clone", lambda work: tmp_path)

    class FakeReview:
        def __init__(self, *a, **k):
            pass

        def execute(self):
            return {"outcome": "reviewed_dry_run", "review": {"event": "COMMENT"}}

        def record(self, rec, dry_run):
            return {"outcome": rec["outcome"], "dry_run": dry_run}

    monkeypatch.setattr(server, "Review", FakeReview)
    monkeypatch.setattr(
        server,
        "publish_stored",
        lambda *a: published.append(a) or {"outcome": "review_posted"},
    )

    out = server.handle_review({"role": "pr_reviewer", "pull_request": {"number": 7}})
    assert (
        out["outcome"] == "reviewed_dry_run"
        and out["dry_run"]
        and not posted
        and not published
    )

    out = server.handle_review(
        {"role": "pr_reviewer", "pull_request": {"number": 7}, "publish": True}
    )
    assert (
        out["outcome"] == "review_posted" and not out["dry_run"] and len(published) == 1
    )
