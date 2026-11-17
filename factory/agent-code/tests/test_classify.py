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
