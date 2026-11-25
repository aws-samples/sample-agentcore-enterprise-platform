"""Guards for the .gitlab-ci.yml header and pipeline shape.

The header must describe the real CI situation: GitHub Actions is the
primary CI and .gitlab-ci.yml is a GitLab reference port.

The job-key assertion is intentionally strict. A PR that adds, removes or
renames a GitLab job must update EXPECTED_KEYS here. That catches
accidental behavior edits made alongside comment-only changes.
"""

from pathlib import Path

import yaml

GITLAB_CI = Path(__file__).resolve().parents[1] / ".gitlab-ci.yml"

EXPECTED_STAGES = ["lint", "validate", "test", "synth", "terraform"]

EXPECTED_KEYS = {
    "ruff",
    "shell",
    "control-library",
    "dockerfile-parse",
    "pytest",
    "cdk-synth",
    "terraform",
    ".pip-cache",
    "workflow",
    "default",
    "variables",
    "stages",
}


def _text():
    return GITLAB_CI.read_text(encoding="utf-8")


def _header():
    lines = []
    for line in _text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def test_no_stale_phrases():
    text = _text()
    assert "never ran" not in text
    assert "~113" not in text


def test_header_mentions_github_actions():
    assert "GitHub Actions" in _header()


def test_stages_unchanged():
    parsed = yaml.safe_load(_text())
    assert parsed["stages"] == EXPECTED_STAGES


def test_job_keys_unchanged():
    parsed = yaml.safe_load(_text())
    assert set(parsed.keys()) == EXPECTED_KEYS
