"""The migration contract: what a `migration:` block promises, and what it refuses.

Every rule here exists because getting it wrong costs a deploy. The two that
matter most: a secret-shaped value must not reach `env` (it renders in clear in
the runtime config), and a private downstream dependency must not be declarable
without the VPC and a path to reach it — that is the Adcubum blocker class, and
config is where it is cheap to catch.
"""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from infra_utils.platform_config import (
    PlatformConfig,
    load_platform_config,
    migration_cutover_ready,
    migration_plan,
    migration_readiness,
    migration_readiness_lines,
    migration_secret_name,
    to_env,
)

# The shape used everywhere below: build here (so arm64 is guaranteed), adapter
# mode, two secrets by NAME.
BASE = {
    "source": {
        "platform": "openshift",
        "build": {"context": "./app", "dockerfile": "Dockerfile"},
        "port": 8000,
        "invoke_path": "/run",
        "health_path": "/healthz",
        "trigger": "webhook",
        "secrets": ["JIRA_TOKEN", "GIT_TOKEN"],
    },
    "target": {"runtime": "agentcore", "mode": "adapter"},
}


def cfg(migration=None, **kw) -> PlatformConfig:
    data = {"project": "proj", "environment": "dev", **kw}
    if migration is not None:
        data["migration"] = migration
    return PlatformConfig(**data)


def deep(*overrides: tuple[str, object]) -> dict:
    """BASE with dotted-path overrides; a value of None deletes the key."""
    import copy

    out = copy.deepcopy(BASE)
    for path, value in overrides:
        node = out
        *parents, leaf = path.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return out


def err(migration, **kw) -> str:
    with pytest.raises(ValidationError) as e:
        cfg(migration, **kw)
    return str(e.value)


# ── the block is optional ────────────────────────────────────────────────────


def test_absent_migration_changes_nothing():
    plain = cfg()
    assert plain.migration is None
    # The footprint and the environment are exactly what they were before the
    # block existed — a migration field must never alter a normal deployment.
    assert plain.expected_stacks("") == cfg().expected_stacks("")
    assert to_env(plain)["MIGRATION_ENABLED"] == "false"
    assert not [
        k
        for k in to_env(plain)
        if k.startswith("MIGRATION_") and k != "MIGRATION_ENABLED"
    ]


def test_happy_path_parses():
    m = cfg(BASE).migration
    assert m.source.platform == "openshift"
    assert m.source.build.dockerfile == "Dockerfile"
    assert m.target.runtime == "agentcore"
    assert m.warnings == []  # built here, so no arm64 doubt


# ── source: image XOR build ──────────────────────────────────────────────────


def test_image_and_build_are_mutually_exclusive():
    both = deep(("source.image", "reg/x:1"))
    assert "exactly one of image" in err(both)


def test_neither_image_nor_build_is_refused():
    assert "exactly one of image" in err(deep(("source.build", None)))


# ── deployed target contract ─────────────────────────────────────────────────


def test_adapter_requires_port_and_path():
    assert "requires migration.source.port" in err(deep(("source.port", None)))
    assert "requires migration.source.port" in err(deep(("source.invoke_path", "")))


def test_native_target_fails_closed_even_with_native_source_shape():
    msg = err(
        deep(
            ("target.mode", "native"), ("source.port", None), ("source.invoke_path", "")
        )
    )
    assert "migration.target.mode 'native' is not deployed" in msg
    assert "only 'adapter' is currently supported" in msg


# ── paths, env and secret names ──────────────────────────────────────────────


def test_paths_must_be_absolute():
    assert "must start with '/'" in err(deep(("source.invoke_path", "run")))
    assert "must start with '/'" in err(deep(("source.health_path", "healthz")))


def test_env_keys_must_be_env_names():
    assert "not an environment variable name" in err(
        deep(("source.env", {"lower": "x"}))
    )


@pytest.mark.parametrize("key", ["JIRA_TOKEN", "DB_PASSWORD", "MY_SECRET", "API_KEY"])
def test_secret_shaped_env_keys_are_refused(key):
    # The whole point: an env value is rendered in clear, a secret is not.
    msg = err(deep(("source.env", {key: "value"})))
    assert "looks like a secret" in msg
    assert "migration.source.secrets" in msg


def test_secrets_are_names_not_values():
    msg = err(deep(("source.secrets", ["ok", "shhh-this-is-a-value"])))
    assert "environment variable NAMES" in msg


def test_secret_manager_name_is_namespaced():
    c = cfg(BASE)
    assert migration_secret_name(c, "JIRA_TOKEN") == "proj/dev/migration/JIRA_TOKEN"


# ── the arm64 warning (non-fatal, deliberately) ──────────────────────────────


def test_prebuilt_image_on_agentcore_warns_but_deploys():
    m = cfg(deep(("source.image", "reg/x:1"), ("source.build", None))).migration
    assert len(m.warnings) == 1
    w = m.warnings[0]
    assert "arm64" in w and "reg/x:1" in w
    assert "migration.source.build" in w
    assert "image@sha256:<digest>" in w
    assert "same-tag repushes are not detected" in w


def test_ec2_target_fails_closed_even_with_networking():
    msg = err(deep(("target.runtime", "ec2")), security={"networking": True})
    assert "migration.target.runtime 'ec2' is not deployed" in msg
    assert "only 'agentcore' is currently supported" in msg


def test_unused_network_extras_warn():
    m = cfg(deep(("network.dns_forwarders", ["10.0.0.2"]))).migration
    assert any("nothing will use them" in w for w in m.warnings)


# ── cross-field rules against security.networking ────────────────────────────


def test_private_dependencies_require_the_vpc():
    msg = err(deep(("network.private_dependencies", ["git.internal"])))
    assert "security.networking: true" in msg


def test_private_dependencies_require_a_path_to_the_customer_network():
    msg = err(
        deep(("network.private_dependencies", ["git.internal"])),
        security={"networking": True},
    )
    assert "connectivity" in msg and "vpn" in msg


def test_private_dependencies_complete_is_accepted():
    c = cfg(
        deep(
            (
                "network.private_dependencies",
                ["git.adcubum.internal", "jira.adcubum.internal"],
            ),
            ("network.connectivity", "vpn"),
            ("network.dns_forwarders", ["10.0.0.2"]),
        ),
        security={"networking": True},
    )
    assert c.migration.network.connectivity == "vpn"
    assert c.migration.warnings == []


def test_hostnames_are_hostnames():
    bad = deep(
        ("network.private_dependencies", ["https://git.internal/path"]),
        ("network.connectivity", "vpn"),
    )
    assert "must be hostnames" in err(bad, security={"networking": True})


def test_dns_forwarders_must_be_ipv4():
    bad = deep(
        ("network.private_dependencies", ["git.internal"]),
        ("network.connectivity", "vpn"),
        ("network.dns_forwarders", ["not-an-ip"]),
    )
    assert "IPv4" in err(bad, security={"networking": True})


def test_private_dependency_probe_scope_is_bounded_and_unique():
    duplicate = deep(
        ("network.private_dependencies", ["git.internal", "git.internal"]),
        ("network.connectivity", "vpn"),
    )
    assert "must not contain duplicates" in err(
        duplicate, security={"networking": True}
    )

    too_many = deep(
        ("network.private_dependencies", [f"host-{i}.internal" for i in range(26)]),
        ("network.connectivity", "vpn"),
    )
    assert "at most 25 items" in err(too_many, security={"networking": True})

    long_host = ".".join(["a" * 60] * 4)
    too_large = deep(
        ("network.private_dependencies", [f"{i}.{long_host}" for i in range(13)]),
        ("network.connectivity", "vpn"),
    )
    assert "maximum encoded size: 3000 bytes" in err(
        too_large, security={"networking": True}
    )


def test_private_ca_field_accepts_only_a_secret_name():
    bad = deep(("network.ca_bundle_secret_name", "not a secret name"))
    assert "Secrets Manager NAME" in err(bad)


# ── staged cutover scope and evidence gates ──────────────────────────────────


COMPLETE_GATE = {
    "owner": "migration-owner",
    "approver": "change-approver",
    "approved_at": "2026-09-21T12:00:00+00:00",
    "evidence": ["EBA-123/network-rehearsal"],
    "rollback": "Restore the recorded source route.",
}


def readiness(migration) -> dict[str, tuple[str, str]]:
    return {
        check.name: (check.status, check.detail)
        for check in migration_readiness(cfg(migration))
    }


def test_cutover_stages_default_out_of_scope_and_not_ready():
    c = cfg(BASE)
    got = readiness(BASE)
    assert got["runtime"][0] == "READY"
    assert got["networking"][0] == "NOT REQUIRED"
    assert got["data"][0] == "NOT REQUIRED"
    assert got["triggers"][0] == "NOT REQUIRED"
    assert got["traffic"][0] == "NOT REQUIRED"
    assert migration_cutover_ready(c) is False
    assert "safe target-only deployment" in "\n".join(migration_readiness_lines(c))


def test_enabled_external_stage_is_blocked_until_gate_is_complete():
    migration = deep(("stages.data.strategy", "external-copy"))
    status, detail = readiness(migration)["data"]
    assert status == "BLOCKED"
    assert all(
        field in detail
        for field in ("owner", "approver", "evidence", "rollback", "approved_at")
    )


def test_complete_external_cutover_is_ready_for_http_source():
    migration = deep(
        ("source.trigger", "http"),
        ("stages.data.strategy", "external-copy"),
        ("stages.data.gate", COMPLETE_GATE),
        ("stages.traffic.strategy", "external-canary"),
        ("stages.traffic.gate", COMPLETE_GATE),
    )
    c = cfg(migration)
    assert migration_cutover_ready(c) is True
    assert "Overall: READY" in "\n".join(migration_readiness_lines(c))


@pytest.mark.parametrize("trigger", ["webhook", "schedule", "queue"])
def test_event_cutover_requires_trigger_shadowing(trigger):
    migration = deep(
        ("source.trigger", trigger),
        ("stages.traffic.strategy", "external-canary"),
    )
    msg = err(migration)
    assert "requires migration.stages.triggers.strategy='external-shadow'" in msg


def test_private_connectivity_becomes_a_mandatory_gate():
    migration = deep(
        ("network.private_dependencies", ["git.internal"]),
        ("network.connectivity", "vpn"),
    )
    c = cfg(migration, security={"networking": True})
    network = {check.name: check for check in migration_readiness(c)}["networking"]
    assert network.status == "BLOCKED"
    assert "approved_at" in network.detail


def test_approved_gate_requires_audit_fields_and_timezone():
    partial = deep(
        ("stages.data.strategy", "external-copy"),
        ("stages.data.gate.approved_at", "2026-09-21T12:00:00+00:00"),
    )
    assert "approved_at requires a complete gate" in err(partial)

    naive = deep(
        ("stages.data.strategy", "external-copy"),
        ("stages.data.gate", {**COMPLETE_GATE, "approved_at": "2026-09-21T12:00:00"}),
    )
    assert "must include a UTC offset" in err(naive)


def test_gate_requires_separate_owner_and_approver():
    migration = deep(
        ("stages.data.strategy", "external-copy"),
        (
            "stages.data.gate",
            {**COMPLETE_GATE, "approver": COMPLETE_GATE["owner"].upper()},
        ),
    )
    assert "owner and approver must be different" in err(migration)


def test_populated_gate_on_out_of_scope_stage_warns():
    m = cfg(deep(("stages.data.gate.owner", "unused-owner"))).migration
    assert any("data.gate is populated" in warning for warning in m.warnings)


# ── to_env(): the names app.py and deploy.sh consume ─────────────────────────


def test_to_env_carries_the_block():
    env = to_env(cfg(BASE))
    assert env["MIGRATION_ENABLED"] == "true"
    assert env["MIGRATION_PORT"] == "8000"
    assert env["MIGRATION_INVOKE_PATH"] == "/run"
    assert env["MIGRATION_HEALTH_PATH"] == "/healthz"
    assert env["MIGRATION_SECRETS"] == "JIRA_TOKEN,GIT_TOKEN"
    assert env["MIGRATION_TARGET_RUNTIME"] == "agentcore"
    assert env["MIGRATION_TARGET_MODE"] == "adapter"
    assert env["MIGRATION_BUILD_CONTEXT"] == "./app"
    # AGENT_PATTERN keeps being emitted: existing consumers must not break.
    assert "AGENT_PATTERN" in env


def test_to_env_serializes_env_as_lossless_json():
    values = {
        "LOG_LEVEL": "info,verbose",
        "QUERY_HINT": "status=ready,owner=platform",
    }
    env = to_env(cfg(deep(("source.env", values))))
    assert json.loads(env["MIGRATION_ENV_JSON"]) == values
    assert env["MIGRATION_ENV_JSON"] == (
        '{"LOG_LEVEL":"info,verbose","QUERY_HINT":"status=ready,owner=platform"}'
    )
    # Kept only while the existing app/runtime consumers are migrated.
    assert "MIGRATION_ENV" in env


# ── migration_plan(): the operator-facing artefact ───────────────────────────


def test_plan_without_a_block_says_so():
    assert "No migration" in migration_plan(cfg())[0]


def test_plan_covers_target_mapping_secrets_and_stacks():
    text = "\n".join(migration_plan(cfg(BASE), ""))
    assert "POST /invocations -> http://127.0.0.1:8000/run" in text
    assert "GET  /ping        -> http://127.0.0.1:8000/healthz" in text
    assert "proj/dev/migration/JIRA_TOKEN" in text
    assert "proj/dev/migration/GIT_TOKEN" in text
    assert "proj-dev-runtime-orchestrator" in text  # the reused stack name
    assert "agents.pattern" in text  # says the pattern is ignored
    assert "Migration cutover readiness:" in text
    assert "safe target-only deployment" in text


@pytest.mark.parametrize("trigger", ["http", "webhook", "schedule", "queue"])
def test_plan_says_trigger_cutover_is_external(trigger):
    text = "\n".join(migration_plan(cfg(deep(("source.trigger", trigger))), ""))
    assert f"source trigger '{trigger}' is discovery metadata only" in text
    assert "trigger/cutover infrastructure is external and is not deployed" in text


def test_plan_prints_a_reachability_check_per_private_dependency():
    c = cfg(
        deep(
            (
                "network.private_dependencies",
                ["git.adcubum.internal", "jira.adcubum.internal"],
            ),
            ("network.connectivity", "vpn"),
        ),
        security={"networking": True},
    )
    text = "\n".join(migration_plan(c, ""))
    for host in ("git.adcubum.internal", "jira.adcubum.internal"):
        assert f"curl -sS -o /dev/null -w '%{{http_code}}' https://{host}/" in text
    assert "does not provision or inject them" in text
    assert "NAT gateway EIP" in text  # the allow-list reminder


def test_plan_surfaces_warnings():
    text = "\n".join(
        migration_plan(
            cfg(deep(("source.image", "reg/x:1"), ("source.build", None))), ""
        )
    )
    assert "Warnings:" in text and "arm64" in text


def test_plan_is_pure_text():
    # No colour codes, no tabs: it goes into a terminal, a log and a doc.
    for line in migration_plan(cfg(BASE), ""):
        assert isinstance(line, str)
        assert "\x1b" not in line and "\t" not in line


# ── the shipped preset ───────────────────────────────────────────────────────


def test_shipped_preset_validates_and_plans(repo_root=None):
    from pathlib import Path

    preset = Path(__file__).resolve().parents[1] / "presets" / "migration.yaml"
    config = load_platform_config(preset)
    assert config.migration is not None
    # It must validate with NO edits: check-contract.sh synthesizes every preset
    # with a fake account, so a REPLACE_ME that fails validation breaks CI.
    text = "\n".join(migration_plan(config, "111111111111"))
    assert "Adapter mapping" in text
    assert config.migration.warnings == []  # the example builds here, on purpose
    # And the example points at the in-repo stand-in, so the docs can be followed.
    raw = yaml.safe_load(preset.read_text())
    assert raw["migration"]["source"]["build"]["context"].endswith("existing-ec2-agent")
