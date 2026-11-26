from __future__ import annotations

import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from scripts.preflight import (
    CommandResult,
    CommandRunner,
    Preflight,
    _assert_read_only_aws_calls,
)

ACCOUNT = "111122223333"


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A private temp dir independent of pytest's basetemp.

    The built-in fixture fails at setup when the configured basetemp parent
    does not exist; these tests only need an empty scratch directory.
    """
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


class FakeRunner:
    def __init__(
        self, overrides: dict[tuple[str, ...], CommandResult] | None = None
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.overrides = overrides or {}

    def run(self, args: Sequence[str]) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        for prefix, result in self.overrides.items():
            if call[: len(prefix)] == prefix:
                return result
        defaults = {
            ("node", "--version"): CommandResult(0, "v20.19.0"),
            ("npm", "--version"): CommandResult(0, "10.8.2"),
            ("aws", "--version"): CommandResult(0, "aws-cli/2.31.0"),
            ("bash", "--version"): CommandResult(0, "GNU bash, version 5.2.37"),
            ("npx", "--no-install", "cdk"): CommandResult(0, "2.1024.0"),
            (
                "aws",
                "sts",
                "get-caller-identity",
            ): CommandResult(
                0,
                f"{ACCOUNT}\tarn:aws:sts::{ACCOUNT}:assumed-role/EbaOperator/session",
            ),
            ("aws", "configure", "get"): CommandResult(0, "us-east-1"),
            ("aws", "secretsmanager", "get-secret-value"): CommandResult(0, "32"),
            ("aws", "bedrock", "get-inference-profile"): CommandResult(
                0, f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/test"
            ),
        }
        for prefix, result in defaults.items():
            if call[: len(prefix)] == prefix:
                return result
        return CommandResult(1, stderr=f"unexpected test command: {call}")


def _which(command: str) -> str | None:
    return None if command == "docker" else f"/test/bin/{command}"


def _manifest(tmp_path: Path, *, account: str = ACCOUNT) -> Path:
    path = tmp_path / "platform.yaml"
    path.write_text(
        f"""
project: preflight-test
environment: dev
region: us-east-1
deployment:
  platform_account: "{account}"
identity:
  idp: entra_id
  tenant_id: "697a5720-ec4f-42dc-9713-d01182b20533"
  client_id: "bec79924-165d-42b5-8ae7-f9f57fc6dbaa"
  client_secret_name: agentcore/test-idp
agents:
  model_id: us.anthropic.claude-sonnet-4-6
""",
        encoding="utf-8",
    )
    return path


def _preflight(path: Path, runner: FakeRunner, repo_root: Path) -> Preflight:
    return Preflight(
        path,
        runner=runner,
        which=_which,
        python_version=(3, 13, 7),
        repo_root=repo_root,
    )


def test_preflight_passes_with_read_only_calls(tmp_path: Path, capsys) -> None:
    runner = FakeRunner()

    result = _preflight(_manifest(tmp_path), runner, tmp_path).run()

    output = capsys.readouterr().out
    assert result == 0
    assert f"matches pinned account {ACCOUNT}" in output
    assert "agentcore/test-idp is readable and non-empty" in output
    assert "is discoverable from us-east-1" in output
    assert "No AWS resources were changed." in output
    _assert_read_only_aws_calls(runner.calls)


def test_preflight_fails_on_missing_secret_without_printing_a_value(
    tmp_path: Path, capsys
) -> None:
    runner = FakeRunner(
        {
            (
                "aws",
                "secretsmanager",
                "get-secret-value",
            ): CommandResult(254, stderr="ResourceNotFoundException")
        }
    )

    result = _preflight(_manifest(tmp_path), runner, tmp_path).run()

    output = capsys.readouterr().out
    assert result == 1
    assert "agentcore/test-idp does not exist" in output
    assert "create or repair it before build" in output
    assert "secret-string" not in output.lower()
    _assert_read_only_aws_calls(runner.calls)


def test_preflight_stops_account_scoped_reads_on_account_mismatch(
    tmp_path: Path, capsys
) -> None:
    runner = FakeRunner()

    result = _preflight(
        _manifest(tmp_path, account="999988887777"), runner, tmp_path
    ).run()

    output = capsys.readouterr().out
    assert result == 1
    assert f"credentials resolve to {ACCOUNT}" in output
    assert "platform.yaml pins 999988887777" in output
    assert not any(call[:2] == ("aws", "secretsmanager") for call in runner.calls)
    assert not any(call[:2] == ("aws", "bedrock") for call in runner.calls)
    _assert_read_only_aws_calls(runner.calls)


def test_preflight_fails_when_migration_build_context_is_missing(
    tmp_path: Path, capsys
) -> None:
    manifest = tmp_path / "migration.yaml"
    manifest.write_text(
        f"""
project: migration-test
environment: eba
region: us-east-1
deployment:
  platform_account: "{ACCOUNT}"
migration:
  source:
    platform: ec2
    build:
      context: ./customer-agent
      dockerfile: Dockerfile
    port: 8000
    invoke_path: /run
  target:
    runtime: agentcore
    mode: adapter
""",
        encoding="utf-8",
    )
    runner = FakeRunner()

    result = _preflight(manifest, runner, tmp_path).run()

    output = capsys.readouterr().out
    assert result == 1
    assert "build context does not exist" in output
    assert "migrated container owns its model configuration" in output
    _assert_read_only_aws_calls(runner.calls)


_NOT_FOUND = {
    ("aws", "secretsmanager", "get-secret-value"): CommandResult(
        254, stderr="ResourceNotFoundException"
    )
}


def _migration_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "migration.yaml"
    manifest.write_text(
        f"""
project: migration-test
environment: eba
region: us-east-1
deployment:
  platform_account: "{ACCOUNT}"
migration:
  source:
    platform: ec2
    build:
      context: ./customer-agent
      dockerfile: Dockerfile
    port: 8000
    invoke_path: /run
    secrets: [JIRA_TOKEN, GIT_TOKEN]
  target:
    runtime: agentcore
    mode: adapter
""",
        encoding="utf-8",
    )
    return manifest


def test_secrets_only_reports_missing_migration_secret_names(
    tmp_path: Path, capsys
) -> None:
    runner = FakeRunner(_NOT_FOUND)

    result = _preflight(
        _migration_manifest(tmp_path), runner, tmp_path
    ).run_secrets_only()

    output = capsys.readouterr().out
    assert result == 1
    assert "Missing or unreadable declared secrets:" in output
    assert "migration-test/eba/migration/JIRA_TOKEN" in output
    assert "migration-test/eba/migration/GIT_TOKEN" in output
    # Only the secret check runs: no local tool probes.
    assert not any(call[0] in {"node", "npm", "npx"} for call in runner.calls)
    _assert_read_only_aws_calls(runner.calls)


def test_secrets_only_reports_missing_idp_secret(tmp_path: Path, capsys) -> None:
    runner = FakeRunner(_NOT_FOUND)

    result = _preflight(_manifest(tmp_path), runner, tmp_path).run_secrets_only()

    output = capsys.readouterr().out
    assert result == 1
    assert "Missing or unreadable declared secrets: agentcore/test-idp" in output


def test_secrets_only_passes_when_secrets_exist(tmp_path: Path, capsys) -> None:
    runner = FakeRunner()

    result = _preflight(
        _migration_manifest(tmp_path), runner, tmp_path
    ).run_secrets_only()

    assert result == 0
    assert "All declared secrets are readable" in capsys.readouterr().out


def test_secrets_only_can_skip_an_idp_secret_about_to_be_upserted(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(_NOT_FOUND)

    result = _preflight(_manifest(tmp_path), runner, tmp_path).run_secrets_only(
        skip_idp=True
    )

    assert result == 0
    assert not any(call[:2] == ("aws", "secretsmanager") for call in runner.calls)


def test_secrets_only_without_manifest_has_nothing_to_check(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = _preflight(tmp_path / "absent.yaml", runner, tmp_path).run_secrets_only()

    assert result == 0
    assert runner.calls == []


def test_deploy_secret_gate_fails_closed_without_dependencies() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    body = script.split("check_declared_secrets() {", 1)[1].split("\n}\n", 1)[0]
    branch = body.split("        3) ", 1)[1].split(";;", 1)[0]
    assert "exit 1" in branch
    assert "skipped" not in body


def test_read_only_guard_rejects_mutating_aws_call() -> None:
    calls = [("aws", "secretsmanager", "create-secret", "--name", "example")]

    try:
        _assert_read_only_aws_calls(calls)
    except AssertionError as exc:
        assert "unapproved AWS call" in str(exc)
    else:
        raise AssertionError("read-only guard accepted create-secret")


def test_command_runner_blocks_unapproved_aws_call_before_execution() -> None:
    result = CommandRunner().run(("aws", "cloudformation", "deploy"))

    assert result.returncode == 1
    assert result.stderr == "blocked non-read-only AWS command"
