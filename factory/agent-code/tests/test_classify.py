"""Golden cases parse and classify (no network). Run: python -m pytest tests -q"""

from pathlib import Path

from factory.ci_fixer import classify

GOLDEN = Path(__file__).resolve().parents[2] / "golden"


def test_secret_boundary_cdk_schema():
    for name in ("gh-42824695223.log", "secret-boundary-cdk-schema.log"):
        t = classify((GOLDEN / name).read_text())
        assert t["category"] == "cdk_cli_schema", name
        assert t["min_cli"].startswith("2.1148."), name
        assert "schema version mismatch" in t["excerpt"]


def test_ruff_i001():
    for name in ("gh-35651579827.log", "ruff-i001.log"):
        t = classify((GOLDEN / name).read_text())
        assert t["category"] == "ruff", name
        assert "I001" in t["codes"]
        assert any(
            f.endswith("migration/simulation/existing-ec2-agent/agent.py")
            for f in t["files"]
        )


def test_format_only_failure_names_its_file():
    t = classify("Would reformat: pkg/a.py\n1 file would be reformatted\n")
    assert (
        t["category"] == "ruff"
        and t["files"] == ["pkg/a.py"]
        and t["format_files"] == ["pkg/a.py"]
    )


def test_edits_outside_the_failing_files_are_refused(tmp_path):
    from factory.ci_fixer import Run

    run = Run.__new__(Run)  # no AWS session needed for apply()
    run.wt, run.work, run.allowed = tmp_path, tmp_path, {"a.py"}
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    notes = run.apply(
        {
            "edits": [
                {"path": "a.py", "old": "x = 1", "new": "x = 2"},
                {"path": "b.py", "old": "y = 1", "new": "y = 2"},
            ],
            "commands": [["ruff", "format", "b.py"]],
        }
    )
    assert (tmp_path / "a.py").read_text() == "x = 2\n"
    assert (tmp_path / "b.py").read_text() == "y = 1\n"
    assert len(notes) == 2 and all("refused" in n for n in notes)
