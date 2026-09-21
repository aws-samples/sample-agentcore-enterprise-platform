"""Private-dependency validation must run inside the target network safely."""

from __future__ import annotations

import importlib.util
import json
import socket
import ssl
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.networking_stack import NetworkingStack

REPO = Path(__file__).resolve().parents[1]
ENV = cdk.Environment(account="111111111111", region="us-east-1")


def load_probe():
    path = REPO / "migration/network-probe" / "handler.py"
    spec = importlib.util.spec_from_file_location(
        "migration_network_probe_handler", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def network_template(**overrides) -> dict[str, Any]:
    app = cdk.App()
    args = {
        "project_name": "migrationtest",
        "environment": "dev",
        "enable_vpc_endpoints": True,
        "migration_private_dependencies": ["git.corp.internal"],
        **overrides,
    }
    stack = NetworkingStack(app, "MigrationNetwork", env=ENV, **args)
    return Template.from_stack(stack).to_json()


def test_probe_is_absent_without_private_dependencies() -> None:
    template = network_template(migration_private_dependencies=[])
    assert not resources(template, "AWS::Lambda::Function")
    parameters = resources(template, "AWS::SSM::Parameter")
    assert not any(
        item["Properties"]["Name"].endswith("migration-dependency-probe-name")
        for item in parameters
    )


def test_probe_uses_the_runtime_vpc_allowlist_and_bounded_logs() -> None:
    template = network_template(
        migration_private_dependencies=["git.corp.internal", "jira.corp.internal"],
        migration_ca_bundle_secret_name="customer/private-ca",
        log_retention_days=90,
        retain_data=True,
    )
    function = resources(template, "AWS::Lambda::Function")[0]
    variables = function["Properties"]["Environment"]["Variables"]

    assert json.loads(variables["MIGRATION_DEPENDENCY_HOSTS_JSON"]) == [
        "git.corp.internal",
        "jira.corp.internal",
    ]
    assert variables["MIGRATION_CA_BUNDLE_SECRET_NAME"] == "customer/private-ca"
    assert function["Properties"]["VpcConfig"]["SecurityGroupIds"]
    assert len(function["Properties"]["VpcConfig"]["SubnetIds"]) == 2
    assert function["Properties"]["Timeout"] == 60

    log_group = resources(template, "AWS::Logs::LogGroup")[0]
    assert log_group["Properties"]["RetentionInDays"] == 90
    assert log_group["DeletionPolicy"] == "Retain"

    statements = [
        statement
        for policy in resources(template, "AWS::IAM::Policy")
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    ca_read = next(
        statement
        for statement in statements
        if statement.get("Sid") == "ReadMigrationCaBundle"
    )
    assert ca_read["Action"] == "secretsmanager:GetSecretValue"
    assert "secret:customer/private-ca-*" in json.dumps(ca_read["Resource"])

    endpoints = resources(template, "AWS::EC2::VPCEndpoint")
    assert any(
        "secretsmanager" in str(endpoint["Properties"]["ServiceName"])
        for endpoint in endpoints
    )


def test_probe_only_uses_declared_hosts_and_returns_no_network_details(
    monkeypatch,
) -> None:
    probe = load_probe()
    seen = []
    monkeypatch.setattr(probe, "HOSTS", ("allowed.internal",))
    monkeypatch.setattr(probe, "tls_context", lambda: object())

    def fake_probe(host, _context):
        seen.append(host)
        return {"host": host, "status": "PASS"}

    monkeypatch.setattr(probe, "probe_host", fake_probe)
    result = probe.handler({"hosts": ["attacker.internal"]}, None)

    assert seen == ["allowed.internal"]
    assert result == {
        "ok": True,
        "results": [{"host": "allowed.internal", "status": "PASS"}],
    }
    assert "address" not in json.dumps(result).lower()
    source = (REPO / "migration/network-probe" / "handler.py").read_text()
    assert "print(" not in source and "logging." not in source


def test_probe_reports_sanitized_dns_and_tls_failures(monkeypatch) -> None:
    probe = load_probe()
    context = SimpleNamespace()

    def no_dns(*_args, **_kwargs):
        raise socket.gaierror

    monkeypatch.setattr(probe.socket, "getaddrinfo", no_dns)
    assert probe.probe_host("missing.internal", context) == {
        "host": "missing.internal",
        "status": "DNS_FAILED",
    }

    monkeypatch.setattr(probe.socket, "getaddrinfo", lambda *_a, **_kw: [object()])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class TlsFailure:
        def wrap_socket(self, *_args, **_kwargs):
            raise ssl.SSLError

    monkeypatch.setattr(
        probe.socket, "create_connection", lambda *_a, **_kw: Connection()
    )
    assert probe.probe_host("bad-cert.internal", TlsFailure()) == {
        "host": "bad-cert.internal",
        "status": "TLS_FAILED",
    }


def test_live_checker_rejects_results_outside_the_manifest(monkeypatch) -> None:
    scripts = REPO / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import check_network
    finally:
        sys.path.remove(str(scripts))

    monkeypatch.setenv("MIGRATION_PRIVATE_DEPENDENCIES", "git.corp.internal")

    class Ssm:
        def get_parameter(self, **_kwargs):
            return {"Parameter": {"Value": "probe"}}

    class Lambda:
        def invoke(self, **_kwargs):
            return {
                "Payload": BytesIO(
                    json.dumps(
                        {
                            "ok": True,
                            "results": [
                                {"host": "attacker.internal", "status": "PASS"}
                            ],
                        }
                    ).encode()
                )
            }

    monkeypatch.setattr(
        check_network.boto3,
        "client",
        lambda service, **_kwargs: Ssm() if service == "ssm" else Lambda(),
    )
    with pytest.raises(SystemExit):
        check_network.check_private_dependencies()
