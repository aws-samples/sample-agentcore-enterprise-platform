"""Synthesized migration infrastructure must match the supported contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.runtime_stack import RuntimeStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")
REPO = Path(__file__).resolve().parents[1]


def resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def migration_template() -> dict[str, Any]:
    app = cdk.App()
    stack = RuntimeStack(
        app,
        "MigrationRuntime",
        project_name="migrationtest",
        environment="dev",
        component_name="orchestrator",
        source_dir="agent-code",
        runtime_type="orchestrator",
        build_context="workshop-simulation/existing-ec2-agent",
        build_dockerfile="Dockerfile",
        adapter_dir="migration-adapter",
        migration_port="8000",
        migration_invoke_path="/run",
        migration_health_path="/healthz",
        migration_env={"CSV_VALUE": "one,two", "EQUALS_VALUE": "x=y"},
        migration_secret_names=["API_TOKEN"],
        env=ENV,
    )
    return Template.from_stack(stack).to_json()


def test_migration_build_preserves_source_user_and_builds_arm64_adapter() -> None:
    template = migration_template()
    buildspec = resources(template, "AWS::CodeBuild::Project")[0]["Properties"][
        "Source"
    ]["BuildSpec"]
    commands = "\n".join(
        command
        for phase in json.loads(buildspec)["phases"].values()
        for command in phase["commands"]
    )

    assert "docker build --platform linux/arm64" in commands
    assert 'Config"].get("User") or "root"' in commands
    assert '--build-arg CHILD_USER="$SOURCE_USER"' in commands
    assert '--build-arg CHILD_CMD="$CHILD_CMD"' in commands
    assert "runtime: ec2" not in commands


def test_rehearsal_source_declares_a_non_root_runtime_user() -> None:
    dockerfile = (
        REPO / "workshop-simulation" / "existing-ec2-agent" / "Dockerfile"
    ).read_text()
    assert "USER 10001:10001" in dockerfile
    assert dockerfile.index("USER 10001:10001") > dockerfile.index(
        "pip install --no-cache-dir"
    )


def test_migration_runtime_uses_lossless_json_env_and_scoped_secrets() -> None:
    template = migration_template()
    runtime = resources(template, "AWS::BedrockAgentCore::Runtime")[0]["Properties"]
    env = runtime["EnvironmentVariables"]

    assert json.loads(env["MIGRATION_ENV_JSON"]) == {
        "CSV_VALUE": "one,two",
        "EQUALS_VALUE": "x=y",
    }
    assert "MIGRATION_ENV" not in env
    assert env["MIGRATION_SECRETS"] == "API_TOKEN"

    policies = resources(template, "AWS::IAM::Policy")
    statements = [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    migration_secrets = next(
        statement
        for statement in statements
        if statement.get("Sid") == "MigrationSecrets"
    )
    assert migration_secrets["Action"] == "secretsmanager:GetSecretValue"
    assert "migration/API_TOKEN-" in json.dumps(migration_secrets["Resource"])
