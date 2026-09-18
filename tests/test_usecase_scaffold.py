"""`deploy.sh usecase new <name>` must produce a use case that loads, is
discovered, is enabled in the manifest without destroying its comments, and
whose verify.py fails the contract way when nothing is deployed.

Every test scaffolds into tmp_path: the repo's own use-cases/ and platform.yaml
are never touched.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess  # nosec B404 — running our own scripts, no shell
import sys
from pathlib import Path

import pytest

from infra_utils.platform_config import USE_CASES_DIR, discover_use_cases

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "usecase.py"

_spec = importlib.util.spec_from_file_location("usecase", SCRIPT)
usecase = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usecase)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A use-cases dir with the template, and a greenfield manifest with comments."""
    uc = tmp_path / "use-cases"
    uc.mkdir()
    shutil.copytree(USE_CASES_DIR / "_template", uc / "_template")
    manifest = tmp_path / "platform.yaml"
    manifest.write_text((REPO / "presets" / "greenfield.yaml").read_text())
    monkeypatch.setenv("PLATFORM_CONFIG", str(manifest))
    return uc, manifest


def run(uc: Path, *args: str) -> int:
    return usecase.main(["--use-cases-dir", str(uc), *args])


def test_template_is_not_a_use_case():
    assert "_template" not in discover_use_cases()
    assert (USE_CASES_DIR / "_template" / "manifest.yaml").exists()


def test_scaffold_creates_the_four_files_and_they_load(sandbox, capsys):
    uc, _ = sandbox
    assert run(uc, "new", "release-notes", "--summary", "Release notes from Jira") == 0
    for f in ("manifest.yaml", "stack.py", "verify.py", "walkthrough.md"):
        assert (uc / "release-notes" / f).exists(), f
    text = (uc / "release-notes" / "manifest.yaml").read_text()
    assert "{{" not in text  # every token filled
    assert "stacks: [uc-release-notes]" in text
    found = discover_use_cases(uc)
    assert found["release-notes"].summary == "Release notes from Jira"
    assert found["release-notes"].stacks == ["uc-release-notes"]
    out = capsys.readouterr().out
    assert "deploy.sh design" in out and "deploy.sh verify" in out


def test_manifest_gains_the_entry_and_keeps_its_comments(sandbox):
    uc, manifest = sandbox
    before = manifest.read_text()
    assert run(uc, "new", "release-notes") == 0
    after = manifest.read_text()
    assert after.startswith(before.rstrip("\n"))  # nothing above was touched
    assert after.rstrip().endswith("use_cases:\n  release-notes: {}")
    assert "# every control opt-in" in after  # a comment survived


def test_second_use_case_joins_the_existing_block(sandbox):
    uc, manifest = sandbox
    assert run(uc, "new", "first-one") == 0
    assert run(uc, "new", "second-one") == 0
    text = manifest.read_text()
    assert text.count("use_cases:") == 1
    assert "  first-one: {}\n  second-one: {}" in text


def test_empty_use_cases_block_is_replaced(sandbox):
    uc, manifest = sandbox
    manifest.write_text(manifest.read_text() + "use_cases: {}   # none yet\n")
    assert run(uc, "new", "only-one") == 0
    text = manifest.read_text()
    assert "use_cases: {}" not in text
    assert "use_cases:  # none yet\n  only-one: {}" in text


def test_dry_run_writes_nothing(sandbox, capsys):
    uc, manifest = sandbox
    before = manifest.read_text()
    assert run(uc, "new", "ghost", "--dry-run") == 0
    assert not (uc / "ghost").exists()
    assert manifest.read_text() == before
    assert "Would create" in capsys.readouterr().out


def test_requires_override_lands_in_the_manifest(sandbox):
    uc, _ = sandbox
    assert run(uc, "new", "needs-memory", "--requires", "gateway,memory") == 0
    assert discover_use_cases(uc)["needs-memory"].requires == ["gateway", "memory"]


@pytest.mark.parametrize("bad", ["Bad_Name", "ab", "has space", "x" * 50])
def test_bad_names_are_refused(sandbox, bad, capsys):
    uc, _ = sandbox
    assert run(uc, "new", bad) == 2
    assert not (uc / bad).exists()
    assert "invalid name" in capsys.readouterr().err


def test_existing_directory_is_refused(sandbox, capsys):
    uc, _ = sandbox
    assert run(uc, "new", "twice") == 0
    assert run(uc, "new", "twice") == 1
    assert "already exists" in capsys.readouterr().err


def test_list_shows_enabled_state(sandbox, capsys):
    uc, _ = sandbox
    run(uc, "new", "shown")
    assert run(uc, "list") == 0
    out = capsys.readouterr().out
    assert "shown" in out and "yes" in out  # the manifest names it


def test_scaffolded_verify_fails_fast_without_a_deployment(sandbox):
    uc, _ = sandbox
    run(uc, "new", "checkme")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": "us-east-1",
    }
    r = subprocess.run(  # nosec B603
        [sys.executable, str(uc / "checkme" / "verify.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r.returncode == 1
    assert r.stderr.startswith("FAIL:"), r.stderr
    assert "Traceback" not in r.stderr


def test_flag_shaped_name_fails_closed(sandbox):
    uc, _ = sandbox
    assert run(uc, "new", "-lead") == 2  # argparse usage error; nothing written
    assert not (uc / "-lead").exists()
