"""The migration contract: what a `migration:` block promises, and what it refuses.

Every rule here exists because getting it wrong costs a deploy. The two that
matter most: a secret-shaped value must not reach `env` (it renders in clear in
the runtime config), and a private downstream dependency must not be declarable
without the VPC and a path to reach it — that is the Adcubum blocker class, and
config is where it is cheap to catch.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from pydantic import ValidationError

from infra_utils.platform_config import (
    PlatformConfig,
    load_platform_config,
    migration_cutover_plan_lines,
    migration_cutover_ready,
    migration_data_plan_document,
    migration_data_plan_lines,
    migration_data_ready,
    migration_network_plan_document,
    migration_plan,
    migration_readiness,
    migration_readiness_lines,
    migration_runtime_plan_document,
    migration_secret_name,
    migration_traffic_plan_document,
    migration_trigger_plan_document,
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


TEST_NOW = datetime.now(timezone.utc)
DEFAULT_APPROVED_AT = (TEST_NOW - timedelta(hours=2)).isoformat()
DEFAULT_EXPIRES_AT = (TEST_NOW + timedelta(hours=2)).isoformat()
COMPLETE_GATE = {
    "owner": "migration-owner",
    "approver": "change-approver",
    "approved_at": DEFAULT_APPROVED_AT,
    "expires_at": DEFAULT_EXPIRES_AT,
    "evidence": ["EBA-123/network-rehearsal"],
    "rollback": "Restore the recorded source route.",
}
ABORT = {
    "max_error_rate_percent": 1.0,
    "max_p95_latency_ms": 5000,
    "max_failed_events": 0,
    "observation_minutes": 15,
}
RUNTIME_RECEIPT = {
    "account_id": "111111111111",
    "runtime_arn": (
        "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/proj-dev-orchestrator"
    ),
    "source_hash": "0123456789abcdef",
    "image_digest": f"sha256:{'a' * 64}",
    "verification_reference": "EBA-123/live-runtime-verification",
}
NETWORK_RECEIPT = {
    "vpc_id": "vpc-0123456789abcdef0",
    "private_subnet_ids": [
        "subnet-0123456789abcdef0",
        "subnet-0123456789abcdef1",
    ],
    "security_group_ids": ["sg-0123456789abcdef0"],
    "probe_verification_reference": "EBA-123/private-dependency-probe",
}


def complete_gate(
    *evidence,
    approved_at=DEFAULT_APPROVED_AT,
    expires_at=DEFAULT_EXPIRES_AT,
):
    return {
        **COMPLETE_GATE,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "evidence": [*COMPLETE_GATE["evidence"], *evidence],
    }


def approve_runtime(migration):
    migration.setdefault("stages", {})["runtime"] = {
        **RUNTIME_RECEIPT,
        "gate": complete_gate(),
    }
    draft = cfg(migration)
    migration["stages"]["runtime"]["gate"]["evidence"].append(
        migration_runtime_plan_document(draft)["digest"]
    )


def trigger_plan(trigger, safety_mode):
    plan = {
        "strategy": "external-shadow",
        "source_reference": f"{trigger.upper()}-SOURCE-123",
        "shadow_reference": f"{trigger.upper()}-SHADOW-123",
        "safety_mode": safety_mode,
        "validation_reference": f"{trigger.upper()}-TEST-123",
    }
    if safety_mode in {"idempotent", "dual-publish"}:
        plan["idempotency_reference"] = f"{trigger.upper()}-IDEMPOTENCY-123"
    if trigger == "webhook":
        plan["signature_validation_reference"] = "WEBHOOK-SIGNATURE-123"
    return plan


def traffic_plan(strategy="external-canary"):
    return {
        "strategy": strategy,
        "router_reference": "ROUTER-123",
        "source_reference": "SOURCE-ROUTE-123",
        "target_reference": "TARGET-ROUTE-123",
        "metrics_reference": "DASHBOARD-123",
        "canary_steps_percent": [5, 25, 100] if strategy == "external-canary" else [],
        "abort": ABORT,
    }


def readiness(migration) -> dict[str, tuple[str, str]]:
    return {
        check.name: (check.status, check.detail)
        for check in migration_readiness(cfg(migration))
    }


def test_cutover_stages_default_out_of_scope_and_not_ready():
    c = cfg(BASE)
    got = readiness(BASE)
    assert got["runtime"][0] == "BLOCKED"
    assert "deployed target receipt" in got["runtime"][1]
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
        for field in (
            "owner",
            "approver",
            "evidence",
            "rollback",
            "approved_at",
            "expires_at",
        )
    )


def test_complete_external_cutover_is_ready_for_http_source():
    migration = deep(
        ("source.trigger", "http"),
        ("stages.traffic", {**traffic_plan(), "gate": complete_gate()}),
    )
    approve_runtime(migration)
    draft = cfg(migration)
    migration["stages"]["traffic"]["gate"]["evidence"].append(
        migration_traffic_plan_document(draft)["digest"]
    )
    c = cfg(migration)
    assert migration_cutover_ready(c) is True
    assert "Overall: READY" in "\n".join(migration_readiness_lines(c))


@pytest.mark.parametrize("trigger", ["webhook", "schedule", "queue"])
def test_event_cutover_requires_trigger_shadowing(trigger):
    migration = deep(
        ("source.trigger", trigger),
        (
            "stages.traffic",
            traffic_plan(
                "external-switch"
                if trigger in {"schedule", "queue"}
                else "external-canary"
            ),
        ),
    )
    msg = err(migration)
    assert "requires migration.stages.triggers.strategy='external-shadow'" in msg


def test_out_of_scope_cutover_stages_reject_plan_fields():
    assert "trigger plan fields require strategy 'external-shadow'" in err(
        deep(("stages.triggers.source_reference", "WEBHOOK-SOURCE-123"))
    )
    assert "traffic plan fields require a non-none strategy" in err(
        deep(("stages.traffic.router_reference", "ROUTER-123"))
    )


@pytest.mark.parametrize(
    ("trigger", "safety_mode"),
    [
        ("http", "read-only"),
        ("webhook", "idempotent"),
        ("schedule", "dry-run"),
        ("queue", "dual-publish"),
    ],
)
def test_trigger_shadow_plan_enforces_source_specific_safety(trigger, safety_mode):
    migration = deep(
        ("source.trigger", trigger),
        ("stages.triggers", trigger_plan(trigger, safety_mode)),
    )
    assert cfg(migration).migration.stages.triggers.safety_mode == safety_mode


def test_trigger_shadow_rejects_unsafe_or_ambiguous_plans():
    unsafe_queue = deep(
        ("source.trigger", "queue"),
        ("stages.triggers", trigger_plan("queue", "read-only")),
    )
    assert "requires safety_mode in ['dual-publish']" in err(unsafe_queue)

    missing_idempotency = trigger_plan("queue", "dual-publish")
    missing_idempotency.pop("idempotency_reference")
    assert "requires idempotency_reference" in err(
        deep(
            ("source.trigger", "queue"),
            ("stages.triggers", missing_idempotency),
        )
    )

    missing_signature = trigger_plan("webhook", "read-only")
    missing_signature.pop("signature_validation_reference")
    assert "requires signature_validation_reference" in err(
        deep(
            ("source.trigger", "webhook"),
            ("stages.triggers", missing_signature),
        )
    )

    same_target = trigger_plan("schedule", "dry-run")
    same_target["shadow_reference"] = same_target["source_reference"]
    assert "source_reference and shadow_reference must be different" in err(
        deep(
            ("source.trigger", "schedule"),
            ("stages.triggers", same_target),
        )
    )


@pytest.mark.parametrize("unsafe", ["REF\tVALUE", "REF\x1b[2J", "REF\x00VALUE"])
def test_cutover_references_reject_terminal_control_characters(unsafe):
    plan = trigger_plan("webhook", "read-only")
    plan["validation_reference"] = unsafe
    assert "printable single-line reference" in err(
        deep(
            ("source.trigger", "webhook"),
            ("stages.triggers", plan),
        )
    )


@pytest.mark.parametrize(
    "steps",
    ([], [25, 5, 100], [5, 5, 100], [5, 25], [0, 100], [5, 101]),
)
def test_percentage_canary_requires_bounded_increasing_steps_ending_at_100(steps):
    plan = traffic_plan()
    plan["canary_steps_percent"] = steps
    assert "external-canary requires unique, increasing" in err(
        deep(
            ("source.trigger", "http"),
            ("stages.traffic", plan),
        )
    )


def test_atomic_switch_rejects_canary_steps_and_same_route():
    with_steps = traffic_plan("external-switch")
    with_steps["canary_steps_percent"] = [100]
    assert "external-switch is atomic" in err(
        deep(
            ("source.trigger", "http"),
            ("stages.traffic", with_steps),
        )
    )

    same_route = traffic_plan()
    same_route["target_reference"] = same_route["source_reference"]
    assert "source_reference and target_reference must be different" in err(
        deep(
            ("source.trigger", "http"),
            ("stages.traffic", same_route),
        )
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_error_rate_percent", True),
        ("max_error_rate_percent", "1"),
        ("max_p95_latency_ms", False),
        ("max_failed_events", "0"),
        ("observation_minutes", True),
    ],
)
def test_abort_thresholds_reject_boolean_and_string_coercion(field, value):
    plan = traffic_plan()
    plan["abort"] = {**ABORT, field: value}
    assert field in err(
        deep(
            ("source.trigger", "http"),
            ("stages.traffic", plan),
        )
    )


def test_canary_steps_reject_boolean_and_string_coercion():
    plan = traffic_plan()
    plan["canary_steps_percent"] = [5, True, 100]
    assert "not booleans or numeric strings" in err(
        deep(
            ("source.trigger", "http"),
            ("stages.traffic", plan),
        )
    )


@pytest.mark.parametrize("trigger", ["schedule", "queue"])
def test_schedule_and_queue_use_an_atomic_switch_after_shadowing(trigger):
    migration = deep(
        ("source.trigger", trigger),
        (
            "stages.triggers",
            trigger_plan(trigger, "dual-publish" if trigger == "queue" else "dry-run"),
        ),
        ("stages.traffic", traffic_plan()),
    )
    assert "cannot use a percentage canary" in err(migration)

    migration["stages"]["traffic"] = traffic_plan("external-switch")
    assert cfg(migration).migration.stages.traffic.strategy == "external-switch"


def test_trigger_and_traffic_readiness_are_bound_to_exact_plan_digests():
    migration = deep(
        ("source.trigger", "webhook"),
        ("stages.triggers", trigger_plan("webhook", "idempotent")),
        ("stages.traffic", traffic_plan()),
    )
    trigger_draft = cfg(migration)
    trigger_digest = migration_trigger_plan_document(trigger_draft)["digest"]
    migration["stages"]["triggers"]["gate"] = complete_gate(trigger_digest)

    approve_runtime(migration)
    traffic_draft = cfg(migration)
    traffic_digest = migration_traffic_plan_document(traffic_draft)["digest"]
    migration["stages"]["traffic"]["gate"] = complete_gate(traffic_digest)

    ready = cfg(migration)
    checks = {check.name: check for check in migration_readiness(ready)}
    assert checks["triggers"].status == "READY"
    assert checks["traffic"].status == "READY"
    assert migration_cutover_ready(ready) is True

    migration["stages"]["traffic"]["canary_steps_percent"] = [10, 50, 100]
    changed = cfg(migration)
    changed_checks = {check.name: check for check in migration_readiness(changed)}
    assert changed_checks["traffic"].status == "BLOCKED"
    assert "exact plan digest" in changed_checks["traffic"].detail
    assert migration_traffic_plan_document(changed)["digest"] != traffic_digest

    migration["stages"]["traffic"]["canary_steps_percent"] = [5, 25, 100]
    migration["stages"]["triggers"]["validation_reference"] = "WEBHOOK-TEST-456"
    changed = cfg(migration)
    changed_checks = {check.name: check for check in migration_readiness(changed)}
    assert changed_checks["triggers"].status == "BLOCKED"
    assert changed_checks["traffic"].status == "BLOCKED"


def test_traffic_approval_must_follow_prerequisite_approvals():
    migration = deep(
        ("source.trigger", "webhook"),
        ("stages.triggers", trigger_plan("webhook", "read-only")),
        ("stages.traffic", traffic_plan()),
    )
    approve_runtime(migration)
    trigger_digest = migration_trigger_plan_document(cfg(migration))["digest"]
    migration["stages"]["triggers"]["gate"] = complete_gate(
        trigger_digest,
        approved_at=(TEST_NOW - timedelta(hours=1)).isoformat(),
    )
    traffic_digest = migration_traffic_plan_document(cfg(migration))["digest"]
    migration["stages"]["traffic"]["gate"] = complete_gate(traffic_digest)
    config = cfg(migration)
    traffic = {check.name: check for check in migration_readiness(config)}["traffic"]
    assert traffic.status == "BLOCKED"
    assert "predates" in traffic.detail
    assert migration_cutover_ready(config) is False


def test_cutover_plan_is_read_only_and_excludes_approval_fields_from_digests():
    migration = deep(
        ("source.trigger", "webhook"),
        ("stages.triggers", trigger_plan("webhook", "read-only")),
        ("stages.traffic", traffic_plan()),
    )
    approve_runtime(migration)
    config = cfg(migration)
    runtime_document = migration_runtime_plan_document(config)
    trigger_document = migration_trigger_plan_document(config)
    traffic_document = migration_traffic_plan_document(config)
    assert "gate" not in runtime_document["receipt"]
    assert "gate" not in trigger_document["plan"]
    assert "gate" not in traffic_document["plan"]
    assert traffic_document["runtimePlanDigest"] == runtime_document["digest"]

    text = "\n".join(migration_cutover_plan_lines(config))
    assert runtime_document["digest"] in text
    assert trigger_document["digest"] in text
    assert traffic_document["digest"] in text
    assert "Steps: 5%, 25%, 100%" in text
    assert "customer-operated" in text
    assert "does not change an event source or router" in text


def test_cutover_remains_blocked_without_a_verified_runtime_receipt():
    migration = deep(
        ("source.trigger", "http"),
        ("stages.traffic", {**traffic_plan(), "gate": complete_gate()}),
    )
    draft = cfg(migration)
    migration["stages"]["traffic"]["gate"]["evidence"].append(
        migration_traffic_plan_document(draft)["digest"]
    )
    config = cfg(migration)
    assert migration_cutover_ready(config) is False
    runtime = {check.name: check for check in migration_readiness(config)}["runtime"]
    assert runtime.status == "BLOCKED"
    assert "account_id" in runtime.detail
    assert "image_digest" in runtime.detail
    assert "verification_reference" in runtime.detail
    assert "PENDING" in "\n".join(migration_cutover_plan_lines(config))


def test_runtime_approval_is_bound_to_account_source_and_deployed_artifact():
    migration = deep(("source.trigger", "http"))
    migration["stages"] = {"runtime": RUNTIME_RECEIPT}
    original = cfg(
        migration,
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
    )
    original_digest = migration_runtime_plan_document(original)["digest"]

    changed_source = deep(
        ("source.trigger", "http"),
        ("source.build.context", "./different-source"),
        ("stages.runtime", RUNTIME_RECEIPT),
    )
    changed = cfg(
        changed_source,
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
    )
    assert migration_runtime_plan_document(changed)["digest"] != original_digest

    changed_effective_config = cfg(
        migration,
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
        agents={"model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0"},
        security={"networking": True},
    )
    assert (
        migration_runtime_plan_document(changed_effective_config)["digest"]
        != original_digest
    )

    changed_observability = cfg(
        migration,
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
        observability={"alarms": True},
    )
    assert (
        migration_runtime_plan_document(changed_observability)["digest"]
        == original_digest
    )

    traffic_migration = deep(
        ("source.trigger", "http"),
        ("stages.runtime", RUNTIME_RECEIPT),
        ("stages.traffic", traffic_plan()),
    )
    original_traffic = cfg(
        traffic_migration,
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
    )
    changed_traffic_observability = cfg(
        traffic_migration,
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
        observability={"alarms": True},
    )
    assert (
        migration_traffic_plan_document(changed_traffic_observability)["digest"]
        != migration_traffic_plan_document(original_traffic)["digest"]
    )

    wrong_account = {
        **RUNTIME_RECEIPT,
        "account_id": "222222222222",
        "runtime_arn": RUNTIME_RECEIPT["runtime_arn"].replace(
            "111111111111", "222222222222"
        ),
    }
    msg = err(
        deep(
            ("source.trigger", "http"),
            ("stages.runtime", wrong_account),
        ),
        deployment={"platform_account": RUNTIME_RECEIPT["account_id"]},
    )
    assert "must match deployment.platform_account" in msg

    placeholder = deep(
        ("source.trigger", "http"),
        ("stages.runtime.account_id", "123456789012"),
    )
    assert "real target AWS account" in err(placeholder)

    wrong_region = deep(
        ("source.trigger", "http"),
        (
            "stages.runtime",
            {
                **RUNTIME_RECEIPT,
                "runtime_arn": RUNTIME_RECEIPT["runtime_arn"].replace(
                    "us-east-1", "eu-west-1"
                ),
            },
        ),
    )
    assert "must use platform region" in err(wrong_region)


def test_network_approval_is_bound_to_dns_ca_and_runtime_account():
    migration = deep(
        ("network.private_dependencies", ["git.internal"]),
        ("network.connectivity", "vpn"),
        ("network.dns_forwarders", ["10.0.0.2"]),
        ("network.ca_bundle_secret_name", "migration/private-ca"),
        (
            "network.receipt",
            {**NETWORK_RECEIPT, "ca_bundle_version_id": "ca-version-123"},
        ),
        ("stages.runtime.account_id", RUNTIME_RECEIPT["account_id"]),
        ("network.gate", complete_gate()),
    )
    draft = cfg(migration, security={"networking": True})
    network_digest = migration_network_plan_document(draft)["digest"]
    migration["network"]["gate"]["evidence"].append(network_digest)
    ready = cfg(migration, security={"networking": True})
    assert {check.name: check for check in migration_readiness(ready)}[
        "networking"
    ].status == "READY"

    migration["network"]["dns_forwarders"] = ["10.0.0.3"]
    changed = cfg(migration, security={"networking": True})
    check = {check.name: check for check in migration_readiness(changed)}["networking"]
    assert check.status == "BLOCKED"
    assert "exact plan digest" in check.detail
    assert migration_network_plan_document(changed)["digest"] != network_digest

    migration["network"]["dns_forwarders"] = ["10.0.0.2"]
    migration["network"]["receipt"]["ca_bundle_version_id"] = "ca-version-456"
    rotated_ca = cfg(migration, security={"networking": True})
    assert migration_network_plan_document(rotated_ca)["digest"] != network_digest
    assert {check.name: check for check in migration_readiness(rotated_ca)}[
        "networking"
    ].status == "BLOCKED"


def test_private_network_readiness_requires_the_deployed_probe_receipt():
    migration = deep(
        ("network.private_dependencies", ["git.internal"]),
        ("network.connectivity", "vpn"),
        ("stages.runtime.account_id", RUNTIME_RECEIPT["account_id"]),
    )
    config = cfg(migration, security={"networking": True})
    network = {check.name: check for check in migration_readiness(config)}["networking"]
    assert network.status == "BLOCKED"
    assert "deployed network/probe receipt" in network.detail
    assert "vpc_id" in network.detail
    assert "probe_verification_reference" in network.detail


def test_private_connectivity_becomes_a_mandatory_gate():
    migration = deep(
        ("network.private_dependencies", ["git.internal"]),
        ("network.connectivity", "vpn"),
        ("network.receipt", NETWORK_RECEIPT),
        ("stages.runtime.account_id", RUNTIME_RECEIPT["account_id"]),
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
        (
            "stages.data.gate",
            {**complete_gate(), "approved_at": "2020-09-21T12:00:00"},
        ),
    )
    assert "must include a UTC offset" in err(naive)

    future = deep(
        ("stages.data.strategy", "external-copy"),
        (
            "stages.data.gate",
            {**complete_gate(), "approved_at": "2099-09-21T12:00:00+00:00"},
        ),
    )
    assert "cannot be in the future" in err(future)

    too_long = deep(
        ("stages.data.strategy", "external-copy"),
        (
            "stages.data.gate",
            complete_gate(
                expires_at=(TEST_NOW + timedelta(days=8)).isoformat(),
            ),
        ),
    )
    assert "must not exceed 7 days" in err(too_long)


def test_expired_gate_blocks_readiness():
    migration = deep(
        ("stages.data.strategy", "external-copy"),
        (
            "stages.data.gate",
            complete_gate(
                approved_at=(TEST_NOW - timedelta(hours=4)).isoformat(),
                expires_at=(TEST_NOW - timedelta(hours=1)).isoformat(),
            ),
        ),
    )
    data = readiness(migration)["data"]
    assert data[0] == "BLOCKED"
    assert "approval expired" in data[1]


def test_gate_requires_separate_owner_and_approver():
    migration = deep(
        ("stages.data.strategy", "external-copy"),
        (
            "stages.data.gate",
            {**complete_gate(), "approver": COMPLETE_GATE["owner"].upper()},
        ),
    )
    assert "owner and approver must be different" in err(migration)


def test_populated_gate_on_out_of_scope_stage_warns():
    m = cfg(deep(("stages.data.gate.owner", "unused-owner"))).migration
    assert any("data.gate is populated" in warning for warning in m.warnings)


# ── source-specific data plan ────────────────────────────────────────────────


def retained_data(*datasets):
    default = {
        "name": "operational-state",
        "adapter": "retain-source-v1",
        "dependency": "database.corp.internal",
        "classification_reference": "DATA-CLASS-123",
        "retention_reference": "RETENTION-123",
        "identity_mapping_reference": "IDENTITY-MAP-123",
        "validation_reference": "DATA-TEST-123",
    }
    selected = list(datasets) or [default]
    return deep(
        ("network.private_dependencies", ["database.corp.internal"]),
        ("network.connectivity", "vpn"),
        ("network.receipt", NETWORK_RECEIPT),
        ("stages.runtime.account_id", RUNTIME_RECEIPT["account_id"]),
        ("stages.data.strategy", "retain-source"),
        ("stages.data.datasets", selected),
    )


def test_retain_source_requires_versioned_datasets_on_declared_dependencies():
    missing = deep(("stages.data.strategy", "retain-source"))
    assert "requires at least one dataset" in err(missing)

    undeclared = retained_data()
    undeclared["network"]["private_dependencies"] = ["other.corp.internal"]
    assert "retain-source datasets must name dependencies" in err(
        undeclared, security={"networking": True}
    )

    c = cfg(retained_data(), security={"networking": True})
    dataset = c.migration.stages.data.datasets[0]
    assert dataset.adapter == "retain-source-v1"
    assert dataset.dependency == "database.corp.internal"


def test_data_plan_is_deterministic_and_contains_no_gate_assertions():
    one = {
        "name": "zeta-state",
        "dependency": "database.corp.internal",
        "classification_reference": "CLASS-Z",
        "retention_reference": "RET-Z",
        "identity_mapping_reference": "IDENT-Z",
        "validation_reference": "TEST-Z",
    }
    two = {
        "name": "alpha-state",
        "dependency": "database.corp.internal",
        "classification_reference": "CLASS-A",
        "retention_reference": "RET-A",
        "identity_mapping_reference": "IDENT-A",
        "validation_reference": "TEST-A",
    }
    first = cfg(retained_data(one, two), security={"networking": True})
    second = cfg(retained_data(two, one), security={"networking": True})
    assert migration_data_plan_document(first) == migration_data_plan_document(second)
    document = migration_data_plan_document(first)
    assert document["digest"].startswith("sha256:")
    assert [item["name"] for item in document["datasets"]] == [
        "alpha-state",
        "zeta-state",
    ]
    assert "gate" not in json.dumps(document).lower()


def test_data_readiness_is_bound_to_the_exact_plan_digest():
    migration = retained_data()
    migration["stages"]["data"]["gate"] = complete_gate()
    blocked = cfg(migration, security={"networking": True})
    assert migration_data_ready(blocked) is False
    assert (
        "exact plan digest"
        in {check.name: check.detail for check in migration_readiness(blocked)}["data"]
    )

    digest = migration_data_plan_document(blocked)["digest"]
    migration["stages"]["data"]["gate"]["evidence"].append(digest)
    migration["network"]["gate"] = complete_gate()
    network_draft = cfg(migration, security={"networking": True})
    migration["network"]["gate"]["evidence"].append(
        migration_network_plan_document(network_draft)["digest"]
    )
    ready = cfg(migration, security={"networking": True})
    assert migration_data_ready(ready) is True
    lines = "\n".join(migration_data_plan_lines(ready))
    assert digest in lines
    assert "Execution: no copy" in lines
    assert "Gate: READY" in lines


def test_data_dataset_references_are_trimmed_single_lines_and_names_unique():
    duplicate = retained_data(
        {
            "name": "same-state",
            "dependency": "database.corp.internal",
            "classification_reference": "CLASS",
            "retention_reference": "RET",
            "identity_mapping_reference": "IDENT",
            "validation_reference": "TEST",
        },
        {
            "name": "same-state",
            "dependency": "database.corp.internal",
            "classification_reference": "CLASS",
            "retention_reference": "RET",
            "identity_mapping_reference": "IDENT",
            "validation_reference": "TEST",
        },
    )
    assert "dataset names must be unique" in err(
        duplicate, security={"networking": True}
    )

    newline = retained_data()
    newline["stages"]["data"]["datasets"][0]["validation_reference"] = "TEST\npayload"
    assert "single-line evidence reference" in err(
        newline, security={"networking": True}
    )


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
