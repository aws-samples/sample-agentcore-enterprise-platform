"""The migration contract: what a `migration:` block promises, and what it refuses.

Every rule here exists because getting it wrong costs a deploy. The two that
matter most: a secret-shaped value must not reach `env` (it renders in clear in
the runtime config), and a private downstream dependency must not be declarable
without the VPC and a path to reach it — that is the Adcubum blocker class, and
config is where it is cheap to catch.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from infra_utils.platform_config import (
    PlatformConfig,
    load_platform_config,
    migration_plan,
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


# ── adapter vs native ────────────────────────────────────────────────────────


def test_adapter_requires_port_and_path():
    assert "requires migration.source.port" in err(deep(("source.port", None)))
    assert "requires migration.source.port" in err(deep(("source.invoke_path", "")))


def test_native_refuses_port_and_path():
    native = deep(("target.mode", "native"))
    msg = err(native)
    assert "already speaks the AgentCore contract" in msg


def test_native_without_port_is_fine():
    m = cfg(
        deep(
            ("target.mode", "native"), ("source.port", None), ("source.invoke_path", "")
        )
    ).migration
    assert m.target.mode == "native"


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
    assert "runtime: ec2" in w  # the escape hatch is named


def test_prebuilt_image_on_ec2_does_not_warn():
    m = cfg(
        deep(
            ("source.image", "reg/x:1"),
            ("source.build", None),
            ("target.runtime", "ec2"),
        ),
        security={"networking": True},
    ).migration
    assert m.warnings == []


def test_unused_network_extras_warn():
    m = cfg(deep(("network.dns_forwarders", ["10.0.0.2"]))).migration
    assert any("nothing will use them" in w for w in m.warnings)


# ── cross-field rules against security.networking ────────────────────────────


def test_ec2_target_requires_the_vpc():
    assert "security.networking: true" in err(deep(("target.runtime", "ec2")))


def test_ec2_target_with_networking_is_accepted_and_pulls_in_the_stack():
    c = cfg(deep(("target.runtime", "ec2")), security={"networking": True})
    assert "proj-dev-networking" in c.expected_stacks("")


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


def test_to_env_joins_env_pairs():
    env = to_env(cfg(deep(("source.env", {"LOG_LEVEL": "info", "REGION_HINT": "eu"}))))
    assert env["MIGRATION_ENV"] == "LOG_LEVEL=info,REGION_HINT=eu"


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
