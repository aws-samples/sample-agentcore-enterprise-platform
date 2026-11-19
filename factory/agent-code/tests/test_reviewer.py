"""Diff parsing and review payload anchoring (no model, no network)."""

from pathlib import Path

from factory import reviewer
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


# --- cost cap and the paths filter (no model, no network) -----------------------------------------

LINE = "x" * 60


def _pr41_diff(sizes: dict[str, int]) -> tuple[str, str]:
    """(numstat, -U0 diff) for purely-added files, the shape of PR #41: big docs, small code."""
    numstat = "\n".join(f"{n}\t0\t{p}" for p, n in sizes.items())
    out = []
    for p, n in sizes.items():
        out += [
            f"diff --git a/{p} b/{p}",
            "--- /dev/null",
            f"+++ b/{p}",
            f"@@ -0,0 +1,{n} @@",
            *([f"+{LINE}"] * n),
        ]
    return numstat, "\n".join(out)


def _review(monkeypatch, paths_diff, cost_cap=None, paths=None):
    """A Review with no __init__: no boto3 session, no repo, no model."""
    monkeypatch.setattr(reviewer, "build_pack", lambda *a, **k: ("pack", []))
    numstat, diff = paths_diff
    r = reviewer.Review.__new__(reviewer.Review)
    r.cost_cap, r.paths = cost_cap, paths
    r.repo, r.branch = Path("/nonexistent"), ""
    r.pr = {
        "number": 41,
        "title": "t",
        "body": "",
        "user": {"login": "omrsamer"},
        "head": {"sha": "f" * 40},
    }
    r.prompts = []
    r.fetch = lambda: "base"

    def git(*a, **k):
        if a[:2] == ("diff", "--numstat"):
            return numstat
        if a[0] == "diff":
            return diff
        return "\n".join([LINE] * 2000)  # git show <head>:<path>

    r.git = git
    r.conventions = lambda head: ""
    r.cost = lambda: 0.0

    def ask(model, system, prompt, max_tokens):
        r.prompts.append((model, prompt))
        return {"summary": "s", "verdict": "approve-worthy", "findings": []}

    r.ask = ask
    return r


def test_large_review_projects_pass_one_and_trims_groups_to_stay_under_the_cap(
    monkeypatch,
):
    """PR #41's shape: 6 doc files + one sensitive workflow, far past the cap if every group runs."""
    paths = {f"kiro/steering/doc{i}.md": 1200 for i in range(6)}
    paths[".github/workflows/ci.yml"] = 9
    r = _review(monkeypatch, _pr41_diff(paths))
    rec = r.execute()

    assert rec["outcome"] == "reviewed_dry_run"
    spent = sum(
        reviewer.project_usd(reviewer.SOL, len(p), reviewer.SOL_OUT_RESERVE)
        for _, p in r.prompts
    )
    assert spent <= reviewer.LARGE_REVIEW_USD, f"projected ${spent:.3f} over the cap"
    # Risk-first: the sensitive workflow is reviewed, the docs the cap could not afford are listed.
    assert any(".github/workflows/ci.yml" in p for _, p in r.prompts)
    assert rec["files_skipped"], "dropped files must be reported, not silently lost"
    assert all(s.endswith(".md") for s in rec["files_skipped"])


def test_review_over_the_cap_is_skipped_and_carries_no_payload_to_post(monkeypatch):
    r = _review(monkeypatch, _pr41_diff({"kiro/steering/doc.md": 1200}), cost_cap=0.01)
    rec = r.execute()

    assert rec["outcome"] == "skipped_over_cap"
    assert rec["projected_usd"] > 0.01 and rec["cap_usd"] == 0.01
    # handle_review posts only on "reviewed_dry_run", and there is no review payload either way.
    assert "review" not in rec
    assert r.prompts == [], "nothing may be paid for once the projection is over cap"


def test_paths_narrows_the_review_before_the_model_is_chosen(monkeypatch):
    """Opt-in: the whole PR is large (two-pass Sol), the narrowed code file alone is a small Astra review."""
    diff = _pr41_diff({"kiro/steering/doc.md": 1200, ".github/workflows/ci.yml": 9})

    wide = _review(monkeypatch, diff)
    wide.execute()
    # Whole PR: the large two-pass path, Sol over every file first.
    assert [m for m, _ in wide.prompts] == [reviewer.SOL, reviewer.ASTRA]
    assert any("kiro/steering/doc.md" in p for _, p in wide.prompts)

    narrow = _review(monkeypatch, diff, paths=[".github/workflows/ci.yml"])
    rec = narrow.execute()
    assert rec["outcome"] == "reviewed_dry_run"
    assert len(narrow.prompts) == 1  # small path: one call
    model, prompt = narrow.prompts[0]
    assert model == reviewer.ASTRA  # sensitive path, and no longer a large diff
    assert "kiro/steering/doc.md" not in prompt
    # A narrowed review discloses its own scope, so nobody reads it as a review of the whole PR.
    assert "**Scope:**" in rec["review"]["body"]
    assert "`.github/workflows/ci.yml`" in rec["review"]["body"]
    assert (
        "**Scope:**" not in (_review(monkeypatch, diff).execute()["review"]["body"])
    )  # unnarrowed reviews say nothing extra


def test_unmatched_paths_review_nothing_rather_than_the_whole_pr(monkeypatch):
    r = _review(monkeypatch, _pr41_diff({"a.py": 3}), paths=["does/not/exist.py"])
    rec = r.execute()
    assert rec["outcome"] == "skipped" and "no changed file matches" in rec["reason"]
    assert r.prompts == []


# --- severity gates the request-changes event ----------------------------------------------------

_ANCHORED = {"app.py": {"added": {4: "print(os.environ)"}, "removed": {}, "hunks": []}}


def _finding(severity, title="t"):
    return {
        "path": "app.py",
        "line": 4,
        "severity": severity,
        "title": title,
        "body": "b",
    }


def test_minor_only_review_comments_instead_of_requesting_changes():
    """A nit is worth saying, not worth blocking a PR on -- even when the model says 'changes needed'."""
    review = build_review(
        {
            "summary": "s",
            "verdict": "changes needed",
            "findings": [_finding("minor"), _finding("nit")],
        },
        _ANCHORED,
        "sha",
        "m",
        [],
    )
    assert review["event"] == "COMMENT"
    assert len(review["comments"]) == 2  # the findings are still posted


def test_one_major_finding_requests_changes():
    review = build_review(
        {
            "summary": "s",
            "verdict": "approve-worthy",  # the model's word does not decide the event
            "findings": [_finding("minor"), _finding("major")],
        },
        _ANCHORED,
        "sha",
        "m",
        [],
    )
    assert review["event"] == "REQUEST_CHANGES"


def test_a_blocker_requests_changes_and_no_findings_comments():
    assert (
        build_review(
            {"summary": "s", "verdict": "", "findings": [_finding("blocker")]},
            _ANCHORED,
            "sha",
            "m",
            [],
        )["event"]
        == "REQUEST_CHANGES"
    )
    assert (
        build_review(
            {"summary": "s", "verdict": "changes needed", "findings": []},
            _ANCHORED,
            "sha",
            "m",
            [],
        )["event"]
        == "COMMENT"
    )
