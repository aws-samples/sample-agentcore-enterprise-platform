"""Read-only customer preflight for an accelerator design.

The command deliberately uses the AWS CLI instead of an SDK session so it
checks the same profile and credential chain that ``deploy.sh`` will use. It
never calls an AWS mutation API and never prints a secret value.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "platform.yaml"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

READ_ONLY_AWS_CALLS = {
    ("bedrock", "get-foundation-model"),
    ("bedrock", "get-inference-profile"),
    ("configure", "get"),
    ("secretsmanager", "get-secret-value"),
    ("sts", "get-caller-identity"),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Run a bounded command without a shell."""

    def run(self, args: Sequence[str]) -> CommandResult:
        if (
            len(args) >= 3
            and args[0] == "aws"
            and (args[1], args[2]) not in READ_ONLY_AWS_CALLS
        ):
            return CommandResult(1, stderr="blocked non-read-only AWS command")
        try:
            completed = subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(1, stderr=type(exc).__name__)
        return CommandResult(
            completed.returncode,
            completed.stdout.strip(),
            completed.stderr.strip(),
        )


class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.warnings = 0
        self.failed = 0

    def pass_(self, subject: str, detail: str) -> None:
        self.passed += 1
        print(f"[PASS] {subject}: {detail}")

    def warn(self, subject: str, detail: str) -> None:
        self.warnings += 1
        print(f"[WARN] {subject}: {detail}")

    def fail(self, subject: str, detail: str) -> None:
        self.failed += 1
        print(f"[FAIL] {subject}: {detail}")

    def summary(self) -> int:
        print()
        print(
            f"Summary: {self.passed} passed, {self.warnings} warning(s), "
            f"{self.failed} failed."
        )
        print("No AWS resources were changed.")
        return 1 if self.failed else 0


class Preflight:
    def __init__(
        self,
        manifest: Path,
        runner: CommandRunner | None = None,
        reporter: Reporter | None = None,
        which: Callable[[str], str | None] = shutil.which,
        python_version: tuple[int, ...] = sys.version_info,
        repo_root: Path = REPO_ROOT,
    ) -> None:
        self.manifest = manifest
        self.runner = runner or CommandRunner()
        self.reporter = reporter or Reporter()
        self.which = which
        self.python_version = python_version
        self.repo_root = repo_root
        self.config = None
        self.account = ""
        self.role: str | None = None

    def run(self) -> int:
        self._check_local_tools()
        if self._load_manifest():
            account_matches = self._check_aws_identity()
            if account_matches:
                self._check_secrets()
                self._check_model()
            self._check_migration_image()
        else:
            self._check_aws_identity(validate_account=False)
        return self.reporter.summary()

    def _check_local_tools(self) -> None:
        major, minor = self.python_version[:2]
        if (major, minor) == (3, 13):
            self.reporter.pass_("Python", f"{major}.{minor}")
        else:
            self.reporter.fail(
                "Python",
                f"{major}.{minor} is active; create the virtual environment with "
                "python3.13",
            )

        self._check_versioned_tool(
            "node",
            ("node", "--version"),
            minimum_major=20,
            supported_majors={20, 22, 24},
        )
        self._check_versioned_tool("npm", ("npm", "--version"))
        self._check_versioned_tool(
            "AWS CLI", ("aws", "--version"), executable="aws", minimum_major=2
        )
        self._check_versioned_tool(
            "Bash", ("bash", "--version"), executable="bash", minimum_major=4
        )

        if not self.which("npx"):
            self.reporter.fail("AWS CDK", "npx is not installed")
        else:
            result = self.runner.run(("npx", "--no-install", "cdk", "--version"))
            if result.returncode == 0:
                self.reporter.pass_("AWS CDK", result.stdout.splitlines()[0])
            else:
                self.reporter.fail(
                    "AWS CDK",
                    "not installed; run `npm install -g aws-cdk` before build",
                )

        if self.which("docker"):
            result = self.runner.run(("docker", "--version"))
            detail = (
                result.stdout.splitlines()[0] if result.returncode == 0 else "found"
            )
            self.reporter.pass_("Docker", f"{detail} (optional)")
        else:
            self.reporter.warn(
                "Docker",
                "not installed; this is fine because CodeBuild builds deployment images",
            )

    def _check_versioned_tool(
        self,
        subject: str,
        args: tuple[str, ...],
        *,
        executable: str | None = None,
        minimum_major: int | None = None,
        supported_majors: set[int] | None = None,
    ) -> None:
        command = executable or args[0]
        if not self.which(command):
            self.reporter.fail(subject, f"{command} is not installed")
            return
        result = self.runner.run(args)
        if result.returncode != 0:
            self.reporter.fail(subject, f"{command} could not report its version")
            return
        detail = (result.stdout or result.stderr).splitlines()[0]
        if minimum_major is not None or supported_majors is not None:
            match = re.search(r"(\d+)(?:\.\d+)", detail)
            major = int(match.group(1)) if match else 0
            if minimum_major is not None and major < minimum_major:
                self.reporter.fail(
                    subject, f"{detail}; version {minimum_major}+ is required"
                )
                return
            if supported_majors is not None and major not in supported_majors:
                supported = ", ".join(str(value) for value in sorted(supported_majors))
                self.reporter.warn(
                    subject,
                    f"{detail}; use a CDK-supported LTS major ({supported})",
                )
                return
        self.reporter.pass_(subject, detail)

    def _load_manifest(self) -> bool:
        if not self.manifest.is_file():
            self.reporter.fail(
                "Manifest",
                f"{self.manifest} does not exist; run "
                "`./scripts/deploy.sh design --profile greenfield`",
            )
            return False
        try:
            from pydantic import ValidationError

            from infra_utils.platform_config import (
                apply_environment_overrides,
                load_platform_config,
            )

            config = load_platform_config(self.manifest)
            self.config = apply_environment_overrides(config)
        except ModuleNotFoundError as exc:
            self.reporter.fail(
                "Manifest",
                f"Python dependency {exc.name!r} is missing; activate .venv and "
                "run `pip install -r requirements.txt`",
            )
            return False
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
            )
            self.reporter.fail(
                "Manifest",
                "validation failed in "
                + (", ".join(field or "platform.yaml" for field in fields)),
            )
            return False
        except (OSError, ValueError):
            self.reporter.fail(
                "Manifest",
                "could not be parsed safely; run `./scripts/deploy.sh design` for details",
            )
            return False

        version_file = self.repo_root / "VERSION"
        version = (
            version_file.read_text(encoding="utf-8").strip()
            if version_file.exists()
            else "development"
        )
        self.reporter.pass_(
            "Manifest",
            f"{self.config.project}-{self.config.environment}, "
            f"{self.config.deployment.mode}, accelerator {version}",
        )
        for warning in self.config.warnings:
            self.reporter.warn("Manifest", warning)
        return True

    def _check_aws_identity(self, *, validate_account: bool = True) -> bool:
        if not self.which("aws"):
            return False
        result = self.runner.run(
            (
                "aws",
                "sts",
                "get-caller-identity",
                "--query",
                "[Account,Arn]",
                "--output",
                "text",
            )
        )
        parts = result.stdout.split(maxsplit=1)
        if result.returncode != 0 or len(parts) != 2:
            self.reporter.fail(
                "AWS credentials",
                "invalid or expired; refresh the configured profile and retry",
            )
            return False
        self.account, identity = parts
        self.reporter.pass_(
            "AWS credentials", f"account {self.account}, identity {identity}"
        )
        if not validate_account or self.config is None:
            return True

        expected = self.config.deployment.platform_account
        strategy = self.config.deployment.strategy
        region = self.config.region
        profile_region = self.runner.run(("aws", "configure", "get", "region"))
        if (
            profile_region.returncode == 0
            and profile_region.stdout
            and profile_region.stdout != region
        ):
            self.reporter.warn(
                "AWS Region",
                f"effective design uses {region}; AWS profile default is "
                f"{profile_region.stdout}",
            )
        else:
            self.reporter.pass_("AWS Region", region)

        if strategy == "federated":
            try:
                self.role = self.config.federated_role(self.account)
            except ValueError:
                self.reporter.fail(
                    "Deployment account",
                    f"{self.account} is not a declared platform or workload account",
                )
                return False
            self.reporter.pass_(
                "Deployment account",
                f"{self.account} is the federated {self.role} side",
            )
        elif expected and self.account != expected:
            self.reporter.fail(
                "Deployment account",
                f"credentials resolve to {self.account}; platform.yaml pins {expected}",
            )
            return False
        elif expected:
            self.reporter.pass_(
                "Deployment account", f"matches pinned account {expected}"
            )
        else:
            self.reporter.warn(
                "Deployment account",
                f"{self.account} is active but platform.yaml does not pin an account",
            )

        return True

    def _required_secrets(self) -> list[tuple[str, str]]:
        assert self.config is not None
        required: list[tuple[str, str]] = []
        if self.role != "workload" and self.config.identity.idp != "cognito":
            required.append(("enterprise IdP", self.config.identity.client_secret_name))
        if self.role == "workload":
            required.append(
                (
                    "federated M2M",
                    self.config.deployment.federation.m2m_client_secret_name,
                )
            )
        migration = self.config.migration
        if migration is not None and self.role != "platform":
            if migration.source.registry_secret_name:
                required.append(
                    ("migration registry", migration.source.registry_secret_name)
                )
            from infra_utils.platform_config import migration_secret_name

            required.extend(
                (f"migration {name}", migration_secret_name(self.config, name))
                for name in migration.source.secrets
            )
            if migration.network.ca_bundle_secret_name:
                required.append(
                    ("migration private CA", migration.network.ca_bundle_secret_name)
                )
        return list(dict.fromkeys(required))

    def _check_secrets(self) -> None:
        assert self.config is not None
        required = self._required_secrets()
        if not required:
            self.reporter.pass_("Secrets Manager", "no pre-created secret is required")
            return
        for label, name in required:
            result = self.runner.run(
                (
                    "aws",
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    name,
                    "--query",
                    "length(SecretString)",
                    "--output",
                    "text",
                    "--region",
                    self.config.region,
                )
            )
            if (
                result.returncode == 0
                and result.stdout.isdigit()
                and int(result.stdout) > 0
            ):
                self.reporter.pass_(
                    f"Secret ({label})",
                    f"{name} is readable and non-empty in {self.config.region}",
                )
                continue
            error = result.stderr.lower()
            if "resourcenotfound" in error or "can't find" in error:
                reason = "does not exist"
            elif "accessdenied" in error or "not authorized" in error:
                reason = "cannot be read by the active identity"
            else:
                reason = "is empty, binary, or could not be validated"
            self.reporter.fail(
                f"Secret ({label})",
                f"{name} {reason} in account {self.account}, Region "
                f"{self.config.region}; create or repair it before build",
            )

    def _effective_model(self) -> str:
        assert self.config is not None
        if self.config.agents.model_id:
            return self.config.agents.model_id
        agent_file = (
            self.repo_root / "agent-code" / self.config.agents.pattern / "agent.py"
        )
        if not agent_file.is_file():
            return ""
        match = re.search(
            r'^DEFAULT_MODEL_ID\s*=\s*["\']([^"\']+)["\']',
            agent_file.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        return match.group(1) if match else ""

    def _check_model(self) -> None:
        assert self.config is not None
        if self.role == "platform":
            self.reporter.pass_(
                "Bedrock model",
                "not required on the federated platform side (runtimes are in workloads)",
            )
            return
        if self.config.migration is not None and not self.config.agents.model_id:
            self.reporter.warn(
                "Bedrock model",
                "the migrated container owns its model configuration; validate its "
                "model and invocation access separately",
            )
            return
        model = self._effective_model()
        if not model:
            self.reporter.fail(
                "Bedrock model",
                "could not determine the effective model; set agents.model_id",
            )
            return
        geography = model.split(".", 1)[0]
        if geography in {"apac", "eu", "global", "us"}:
            args = (
                "aws",
                "bedrock",
                "get-inference-profile",
                "--inference-profile-identifier",
                model,
                "--query",
                "inferenceProfileArn",
                "--output",
                "text",
                "--region",
                self.config.region,
            )
        else:
            args = (
                "aws",
                "bedrock",
                "get-foundation-model",
                "--model-identifier",
                model,
                "--query",
                "modelDetails.modelArn",
                "--output",
                "text",
                "--region",
                self.config.region,
            )
        result = self.runner.run(args)
        if result.returncode == 0 and result.stdout and result.stdout != "None":
            self.reporter.pass_(
                "Bedrock model",
                f"{model} is discoverable from {self.config.region}; "
                "`verify` proves actual invocation after build",
            )
            return
        self.reporter.fail(
            "Bedrock model",
            f"{model} could not be discovered in {self.config.region}; confirm the "
            "model ID, Region, IAM read access, and Bedrock access",
        )

    def _check_migration_image(self) -> None:
        assert self.config is not None
        migration = self.config.migration
        if migration is None:
            return
        source = migration.source
        if source.build is not None:
            context = (self.repo_root / source.build.context).resolve()
            try:
                context.relative_to(self.repo_root.resolve())
            except ValueError:
                self.reporter.fail(
                    "Migration image",
                    "build context resolves outside the repository",
                )
                return
            dockerfile = (context / source.build.dockerfile).resolve()
            try:
                dockerfile.relative_to(context)
            except ValueError:
                self.reporter.fail(
                    "Migration image",
                    "Dockerfile resolves outside the migration build context",
                )
                return
            if not context.is_dir():
                self.reporter.fail(
                    "Migration image", f"build context does not exist: {context}"
                )
            elif not dockerfile.is_file():
                self.reporter.fail(
                    "Migration image", f"Dockerfile does not exist: {dockerfile}"
                )
            else:
                self.reporter.pass_(
                    "Migration image",
                    "source context and Dockerfile exist; CodeBuild targets linux/arm64",
                )
        else:
            self.reporter.warn(
                "Migration image",
                f"{source.image} is pre-built; confirm it has a linux/arm64 manifest "
                "and pin an immutable digest before customer use",
            )


def _assert_read_only_aws_calls(calls: Sequence[Sequence[str]]) -> None:
    """Defence in depth for tests and future refactors."""
    for call in calls:
        if len(call) < 3 or call[0] != "aws":
            continue
        if (call[1], call[2]) not in READ_ONLY_AWS_CALLS:
            raise AssertionError(f"preflight attempted an unapproved AWS call: {call}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check local tools and AWS readiness without changing resources."
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="platform.yaml path"
    )
    args = parser.parse_args(argv)
    print("AgentCore accelerator doctor (read-only)")
    print(f"Manifest: {args.manifest}")
    print()
    return Preflight(args.manifest).run()


if __name__ == "__main__":
    raise SystemExit(main())
