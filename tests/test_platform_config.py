"""platform.yaml schema: presets stay valid, and validation accumulates errors.

The whole point of the config file is that a participant learns EVERYTHING
wrong with their file in one pass, before any AWS call. These tests pin that
property, plus the cross-field rules that encode real deployment constraints.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from infra_utils.platform_config import (
    AGENT_PATTERNS,
    PlatformConfig,
    load_platform_config,
)

REPO = Path(__file__).resolve().parents[1]
PRESETS = sorted((REPO / "presets").glob("*.yaml"))


def test_presets_exist():
    names = {p.stem for p in PRESETS}
    assert names == {
        "greenfield",
        "migration",
        "multi-agent",
        "platform-team",
        "security-focused",
    }


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.stem)
def test_every_preset_validates(preset):
    config = load_platform_config(preset)
    assert config.project
    assert config.agents.pattern in AGENT_PATTERNS


def test_validation_accumulates_errors():
    """Three problems in, three errors out — not one per deploy cycle."""
    broken = {
        "project": "X",  # violates the lowercase pattern
        "region": "mars-central-1x",  # not a region shape
        "agents": {"pattern": "skynet"},  # not a pattern
    }
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate(broken)
    locs = {e["loc"][0] for e in excinfo.value.errors()}
    assert {"project", "region", "agents"} <= locs, (
        f"expected all three errors reported together, got: {excinfo.value.errors()}"
    )


def test_typoed_keys_are_errors_not_noops():
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate({"projcet": "oops"})
    assert "projcet" in str(excinfo.value)


def test_federated_requires_both_account_fields():
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate({"deployment": {"strategy": "federated"}})
    msg = str(excinfo.value)
    assert "platform_account" in msg


def test_federated_with_accounts_validates():
    config = PlatformConfig.model_validate(
        {
            "deployment": {
                "strategy": "federated",
                "platform_account": "111122223333",
                "workload_accounts": ["444455556666"],
            }
        }
    )
    assert config.deployment.strategy == "federated"


def test_account_ids_must_be_twelve_digits():
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate(
            {
                "deployment": {
                    "strategy": "federated",
                    "platform_account": "12345",
                    "workload_accounts": ["not-an-account"],
                }
            }
        )
    msg = str(excinfo.value)
    assert "12-digit" in msg


def test_traceability_requires_cloudtrail():
    """Encodes TESTING.md caveat 5: alerting without a trail is silence."""
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate({"security": {"traceability": True}})
    assert "cloudtrail_alerting" in str(excinfo.value)


def test_non_cognito_idp_requires_secret_name():
    with pytest.raises(ValidationError) as excinfo:
        PlatformConfig.model_validate(
            {"identity": {"idp": "okta", "issuer_url": "https://x.okta.com"}}
        )
    assert "client_secret_name" in str(excinfo.value)


@pytest.mark.parametrize(
    ("mode", "region", "expected"),
    [
        ("auto", "us-east-1", True),
        ("auto", "eu-central-1", False),
        ("on", "eu-central-1", True),  # explicit on overrides the region gate
        ("off", "us-east-1", False),
    ],
)
def test_web_search_auto_resolves_by_region(mode, region, expected):
    config = PlatformConfig.model_validate(
        {"region": region, "gateway": {"web_search": mode}}
    )
    assert config.web_search_enabled is expected


def test_defaults_are_a_valid_deployment():
    """An EMPTY platform.yaml must be deployable (greenfield defaults)."""
    config = PlatformConfig.model_validate({})
    assert config.deployment.strategy == "centralized"
    assert config.agents.pattern == "orchestrator"
    assert config.security.networking is False
    assert config.observability.transaction_search is True


def test_secrets_never_belong_in_the_file():
    """The schema has no field that could hold a secret VALUE — only the
    Secrets Manager NAME. If someone adds one, this forces the conversation."""
    field_names = set()

    def walk(model):
        for name, field in model.model_fields.items():
            field_names.add(name)
            annotation = field.annotation
            if hasattr(annotation, "model_fields"):
                walk(annotation)

    walk(PlatformConfig)
    forbidden = {n for n in field_names if "secret" in n and not n.endswith("_name")}
    assert not forbidden, f"schema fields that could hold secret values: {forbidden}"


def test_preset_yaml_matches_schema_dump_roundtrip():
    """Presets contain no key the schema doesn't know (extra=forbid catches it
    at load; this asserts the loader is actually exercised on real files)."""
    for preset in PRESETS:
        raw = yaml.safe_load(preset.read_text())
        assert PlatformConfig.model_validate(raw)


def test_to_env_maps_onto_the_names_deploy_already_uses():
    from infra_utils.platform_config import to_env

    env = to_env(load_platform_config(REPO / "presets" / "security-focused.yaml"))
    assert env["ENABLE_NETWORKING"] == "true"
    assert env["ENABLE_CEDAR"] == "true"
    assert env["CEDAR_MODE"] == "LOG_ONLY"
    assert env["ENABLE_SECURITY"] == "true"  # schema name: cloudtrail_alerting
    assert env["AGENT_PATTERN"] == "orchestrator"
    assert env["ORG_ID"] == "o-REPLACEME"
    # web_search auto in us-east-1 resolves to on
    assert env["ENABLE_WEB_SEARCH"] == "true"


def test_to_env_omits_empty_values():
    """Empty strings must not cross the shell boundary: deploy.sh fill-if-unset
    would treat them as set and skip workshop.env / prompts."""
    from infra_utils.platform_config import to_env

    env = to_env(PlatformConfig.model_validate({}))
    assert "MODEL_ID" not in env  # empty = pattern default, not ""
    assert "IDP_TENANT_ID" not in env
    assert all(v != "" for v in env.values())


def test_app_py_resolves_through_cfg():
    """Static guard: every legacy `try_get_context(...) or environ.get(...)`
    lookup in app.py must go through cfg() so platform.yaml participates.
    OAuth provider secrets are the deliberate exception (secrets never live
    in the config file)."""
    src = (REPO / "app.py").read_text()
    allowed = (
        "google_client",
        "github_client",
        "notion_client",
        "platform_config",
        "region",  # region merges CDK_DEFAULT_REGION explicitly
        "CDK_DEFAULT_ACCOUNT",
        "env_key",  # cfg()'s own body
    )
    stale = []
    for i, line in enumerate(src.splitlines(), 1):
        if (
            "environ.get(" in line
            and "cfg(" not in line
            and not any(a in line or a in src.splitlines()[i - 2] for a in allowed)
        ):
            stale.append(f"app.py:{i}: {line.strip()}")
    assert not stale, (
        "these app.py lookups bypass cfg() and so ignore platform.yaml:\n"
        + "\n".join(stale)
    )


# ── Federated strategy (PR C) ──────────────────────────────────────────────

FED = {
    "deployment": {
        "strategy": "federated",
        "platform_account": "111122223333",
        "workload_accounts": ["444455556666"],
        "federation": {
            "gateway_url": "https://example.gateway/mcp",
            "issuer_url": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_X",
            "m2m_client_id": "client123",
            "m2m_client_secret_name": "agentcore/platform-m2m",  # pragma: allowlist secret — a NAME
        },
    }
}


def test_federated_role_by_account():
    config = PlatformConfig.model_validate(FED)
    assert config.federated_role("111122223333") == "platform"
    assert config.federated_role("444455556666") == "workload"


def test_federated_role_wrong_account_is_a_hard_error():
    config = PlatformConfig.model_validate(FED)
    with pytest.raises(ValueError) as excinfo:
        config.federated_role("999999999999")
    assert "neither" in str(excinfo.value)


def test_federated_role_is_none_for_other_strategies():
    config = PlatformConfig.model_validate({})
    assert config.federated_role("111122223333") is None


def test_federation_block_completeness_and_derived_discovery():
    config = PlatformConfig.model_validate(FED)
    federation = config.deployment.federation
    assert federation.is_complete
    assert federation.discovery_url.endswith("/.well-known/openid-configuration")
    incomplete = PlatformConfig.model_validate(
        {
            "deployment": {
                "strategy": "federated",
                "platform_account": "111122223333",
                "workload_accounts": ["444455556666"],
            }
        }
    )
    assert not incomplete.deployment.federation.is_complete


def test_app_gates_stacks_by_federated_role():
    """Static guard: the stack graph must consult the role. If someone
    unconditionally re-instantiates auth/gateway (platform-only) or the
    runtimes (workload-only), federation silently degrades to centralized."""
    src = (REPO / "app.py").read_text()
    assert "is_fed_workload" in src and "is_fed_platform" in src
    assert src.index("federated_role") < src.index("AuthStack(")
    # auth + gateway skipped in workload accounts
    assert "if not is_fed_workload:" in src
    # runtimes skipped in the platform account
    assert "if not is_fed_platform:" in src
