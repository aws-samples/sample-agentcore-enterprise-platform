"""Checks the CI workflows: full unit-test suite, blocking ASH gate, and
the trust boundary of the ASH comment workflow."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "unit-tests.yml"
GITLAB_CI = ROOT / ".gitlab-ci.yml"
ASH_WORKFLOWS = ("ash-security-scan.yml", "ash-full-repository-scan.yml")
ASH_ALLOWLIST = ".ash-suppressions.yaml"
PINNED_USES = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")
CDK_INSTALL = re.compile(r'npm install -g "?aws-cdk([^\s"]*)')


def _load():
    assert WORKFLOW.is_file(), f"missing {WORKFLOW}"
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(data):
    # PyYAML parses a bare `on:` key as boolean True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _steps(data):
    steps = []
    for job in data["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def test_triggers_on_pull_request_and_push():
    triggers = _triggers(_load())
    assert "pull_request" in triggers
    assert "push" in triggers


def test_no_path_filters():
    triggers = _triggers(_load())
    for name in ("pull_request", "push"):
        cfg = triggers.get(name) or {}
        assert "paths" not in cfg, name
        assert "paths-ignore" not in cfg, name


def test_push_targets_main():
    push = _triggers(_load())["push"] or {}
    assert "main" in push.get("branches", [])


def test_actions_pinned_by_sha():
    uses = [s["uses"] for s in _steps(_load()) if "uses" in s]
    assert uses
    for ref in uses:
        assert PINNED_USES.match(ref), ref


def test_runs_full_pytest_suite():
    runs = [s.get("run", "") for s in _steps(_load())]
    assert any("python -m pytest tests/" in r for r in runs)


def _workflow(name):
    path = WORKFLOWS / name
    assert path.is_file(), f"missing {path}"
    return yaml.safe_load(path.read_text())


def _gitlab():
    assert GITLAB_CI.is_file(), f"missing {GITLAB_CI}"
    return yaml.safe_load(GITLAB_CI.read_text())


def _gitlab_commands(job):
    cmds = []
    for key in ("before_script", "script"):
        cmds.extend(job.get(key, []))
    return cmds


def test_cdk_cli_pinned_and_matches_gitlab():
    runs = "\n".join(s.get("run", "") for s in _steps(_load()))
    pins = CDK_INSTALL.findall(runs)
    assert pins
    for pin in pins:
        assert re.fullmatch(r"@\d+\.\d+\.\d+", pin), pin
    gitlab = _gitlab()
    assert gitlab["variables"]["CDK_CLI_VERSION"] == pins[0][1:]
    gitlab_pins = []
    for job in gitlab.values():
        if isinstance(job, dict):
            for cmd in _gitlab_commands(job):
                gitlab_pins.extend(CDK_INSTALL.findall(cmd))
    assert gitlab_pins
    for pin in gitlab_pins:
        assert pin == "@${CDK_CLI_VERSION}", pin


def test_gitlab_pytest_installs_requirements():
    job = _gitlab()["pytest"]
    before = " ".join(job["before_script"])
    assert "-r requirements.txt" in before
    assert "pytest" in before
    assert any("python -m pytest tests/" in c for c in job["script"])


@pytest.mark.parametrize("name", ASH_WORKFLOWS)
def test_ash_scan_is_blocking(name):
    data = _workflow(name)
    steps = _steps(data)
    for step in steps:
        assert not step.get("continue-on-error"), step.get("name")
    scans = [s for s in steps if re.search(r"^ash ", s.get("run", ""), re.MULTILINE)]
    assert len(scans) == 1
    run = scans[0]["run"]
    assert "set -o pipefail" in run
    assert "severity_threshold: HIGH" in run
    assert ASH_ALLOWLIST in run
    gates = [
        s
        for s in steps
        if "HAS_FINDINGS" in s.get("run", "") and "exit 1" in s.get("run", "")
    ]
    assert gates
    for gate in gates:
        assert "always()" in gate.get("if", "")


def _ash_scan_run(name):
    steps = _steps(_workflow(name))
    scans = [s for s in steps if re.search(r"^ash ", s.get("run", ""), re.MULTILINE)]
    assert len(scans) == 1
    return scans[0]["run"]


def _heredoc(run, opener, terminator):
    lines = run.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip().endswith(opener))
    end = lines.index(terminator, start + 1)
    return "\n".join(lines[start + 1 : end]) + "\n"


def _merge_allowlist(name, entries):
    """Run the workflow's merge snippet; return (result, merged config dict)."""
    run = _ash_scan_run(name)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config = tmp_dir / ".ash_config.yaml"
        config.write_text(_heredoc(run, "<< 'EOF'", "EOF"))
        allowlist = tmp_dir / ASH_ALLOWLIST
        allowlist.write_text(yaml.safe_dump({"suppressions": entries}))
        result = subprocess.run(
            [sys.executable, "-", str(allowlist), str(config)],
            input=_heredoc(run, "<<'PY'", "PY"),
            text=True,
            capture_output=True,
            check=False,
        )
        return result, yaml.safe_load(config.read_text())


@pytest.mark.parametrize("name", ASH_WORKFLOWS)
def test_ash_suppressions_merged_into_global_settings(name):
    assert ">> .ash_config.yaml" not in _ash_scan_run(name)
    entry = {
        "rule_id": "B105",
        "file_path": "tests/**",
        "reason": "test fixtures use dummy credentials",
        "expiration": "2026-12-31",
    }
    result, cfg = _merge_allowlist(name, [entry])
    assert result.returncode == 0, result.stderr
    assert "suppressions" not in cfg
    assert cfg["global_settings"]["severity_threshold"] == "HIGH"
    assert cfg["global_settings"]["suppressions"] == [entry]
    assert cfg["reporters"]["markdown"]["enabled"] is True


@pytest.mark.parametrize("name", ASH_WORKFLOWS)
def test_ash_suppressions_reject_path_key(name):
    entry = {"rule_id": "B105", "path": "tests/**", "reason": "dummy"}
    result, _ = _merge_allowlist(name, [entry])
    assert result.returncode != 0
    assert "file_path" in result.stderr


def test_pr_scan_does_not_publish_pr_identity():
    text = (WORKFLOWS / "ash-security-scan.yml").read_text()
    assert "pr_number.txt" not in text
    assert "pr_sha.txt" not in text


def test_comment_workflow_derives_pr_from_event():
    text = (WORKFLOWS / "ash-security-comment.yml").read_text()
    assert "pr_number.txt" not in text
    assert "pr_sha.txt" not in text
    assert "workflow_run.pull_requests" in text
    assert "head_sha" in text
    assert "MAX_COMMENT_BYTES" in text


def test_comment_workflow_does_not_interpolate_artifact():
    data = _workflow("ash-security-comment.yml")
    for step in _steps(data):
        assert "ash-results" not in step.get("run", ""), step.get("name")
