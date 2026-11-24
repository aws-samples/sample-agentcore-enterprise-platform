"""Offline checks that scripts/test_memory.py is safe for pytest collection."""

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("colorama")
pytest.importorskip("boto3")
pytest.importorskip("botocore")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "test_memory.py"


@pytest.fixture
def memory_script(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "memory_script_under_check", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_only_finds_nothing_without_errors():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "scripts/test_memory.py",
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 5, output
    assert "error" not in output


def test_no_test_prefixed_functions(memory_script):
    offenders = [
        name
        for name, value in vars(memory_script).items()
        if name.startswith("test") and inspect.isfunction(value)
    ]
    assert offenders == []


def test_run_checks_wiring_with_mock_client(memory_script, monkeypatch):
    monkeypatch.setattr(memory_script, "print_msg", lambda *a, **k: None)
    monkeypatch.setattr(memory_script, "print_section", lambda *a, **k: None)
    monkeypatch.setattr(memory_script.time, "sleep", lambda _s: None)

    result = memory_script.run_checks(MagicMock(), "mem-123")

    assert isinstance(result, tuple)
    passed, failed = result
    assert passed + failed == 5
