"""Placeholders must fail at parse time, naming the field and the fix.

The bug this pins: the shipped migration preset carried a zero-UUID tenant and
`client_id: REPLACE_ME`, validated, deployed green, and failed at the first
sign-in an hour later with an IdP error that said nothing about the config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from infra_utils.platform_config import PlatformConfig, load_platform_config

REPO = Path(__file__).resolve().parents[1]
PRESETS = REPO / "presets"


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    # Every test here runs as a real deploy would: placeholders are errors.
    monkeypatch.delenv("PLATFORM_ALLOW_PLACEHOLDERS", raising=False)


def cfg(**kw) -> PlatformConfig:
    return PlatformConfig(project="proj", environment="dev", **kw)


def err(**kw) -> str:
    """The validators' own messages — not str(exc), which echoes the input."""
    with pytest.raises(ValidationError) as e:
        cfg(**kw)
    return "\n".join(x["msg"] for x in e.value.errors())


ENTRA = {"idp": "entra_id", "client_secret_name": "agentcore/idp-client-secret"}


# ── identity ─────────────────────────────────────────────────────────────────


def test_zero_uuid_tenant_is_a_placeholder():
    msg = err(
        identity={
            **ENTRA,
            "tenant_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
            "client_id": "abc",
        }
    )
    assert "identity.tenant_id is a placeholder" in msg
    assert "Entra tenant" in msg


def test_non_uuid_tenant_is_refused():
    msg = err(identity={**ENTRA, "tenant_id": "my-tenant", "client_id": "abc"})
    assert "identity.tenant_id is a placeholder" in msg


@pytest.mark.parametrize(
    "cid", ["REPLACE_ME", "CHANGE_ME", "change-me", "TODO", "<your-client-id>"]
)
def test_sentinel_client_ids_are_refused(cid):
    tenant = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    msg = err(identity={**ENTRA, "tenant_id": tenant, "client_id": cid})
    assert "identity.client_id is a placeholder" in msg
    assert cid in msg


def test_federated_idp_requires_a_client_id():
    tenant = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    assert "requires identity.client_id" in err(identity={**ENTRA, "tenant_id": tenant})


def test_issuer_url_must_be_https_and_real():
    okta = {"idp": "okta", "client_id": "abc", "client_secret_name": "agentcore/okta"}
    assert "issuer_url is a placeholder" in err(
        identity={**okta, "issuer_url": "http://x.okta.com"}
    )
    assert "issuer_url is a placeholder" in err(
        identity={**okta, "issuer_url": "https://example.com/oauth2"}
    )


@pytest.mark.parametrize(
    "value",
    [
        "REPLACE_ME",
        "has a space",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",  # a JWT pasted as the name
        "Q2xpZW50U2VjcmV0VmFsdWVUaGF0SXNSZWFsbHlMb25n",  # a base64 blob
    ],
)
def test_secret_name_that_looks_like_a_value_is_refused(value):
    tenant = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    msg = err(
        identity={
            "idp": "entra_id",
            "tenant_id": tenant,
            "client_id": "abc",
            "client_secret_name": value,
        }
    )
    assert "identity.client_secret_name" in msg
    assert value not in msg  # never echo what might BE the secret


def test_real_identity_values_pass():
    c = cfg(
        identity={
            "idp": "entra_id",
            "tenant_id": "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "client_id": "9a8b7c6d-1234-4abc-9def-000111222333",
            "client_secret_name": "agentcore/idp-client-secret",
        }
    )
    assert c.warnings == []


# ── deployment ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("acct", ["000000000000", "123456789012"])
def test_sentinel_platform_account_is_refused(acct):
    msg = err(
        deployment={
            "strategy": "federated",
            "platform_account": acct,
            "workload_accounts": ["222233334444"],
        }
    )
    assert "deployment.platform_account is a placeholder" in msg
    assert "get-caller-identity" in msg


def test_sentinel_workload_account_is_refused_by_index():
    msg = err(
        deployment={
            "strategy": "federated",
            "platform_account": "222233334444",
            "workload_accounts": ["333344445555", "000000000000"],
        }
    )
    assert "deployment.workload_accounts[1] is a placeholder" in msg


def test_word_shaped_account_is_a_hard_error_even_for_the_gate(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    msg = err(
        deployment={
            "strategy": "federated",
            "platform_account": "REPLACE_ME",
            "workload_accounts": ["222233334444"],
        }
    )
    assert "deployment.platform_account is a placeholder" in msg


def test_parity_gate_account_is_not_a_sentinel():
    # 111111111111 is what check-contract.sh synthesizes with; a real customer
    # cannot be allocated a repeating-digit id, so accepting it costs nothing.
    c = cfg(
        deployment={
            "strategy": "federated",
            "platform_account": "111111111111",
            "workload_accounts": ["222233334444"],
        }
    )
    assert c.warnings == []


def test_an_account_cannot_be_both_sides():
    msg = err(
        deployment={
            "strategy": "federated",
            "platform_account": "222233334444",
            "workload_accounts": ["222233334444"],
        }
    )
    assert "both deployment.platform_account and in deployment.workload_accounts" in msg


# ── the gate's escape hatch ──────────────────────────────────────────────────


def test_env_downgrades_placeholders_to_warnings(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    c = cfg(
        identity={
            **ENTRA,
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "client_id": "REPLACE_ME",
        },
        deployment={
            "strategy": "federated",
            "platform_account": "000000000000",
            "workload_accounts": ["123456789012"],
        },
        security={"org_id": "o-example1234"},
        observability={"alarm_email": "ops@REPLACE_ME.invalid"},
    )
    joined = "\n".join(c.warnings)
    for field in (
        "identity.tenant_id",
        "identity.client_id",
        "deployment.platform_account",
        "deployment.workload_accounts[0]",
        "security.org_id",
        "observability.alarm_email",
    ):
        assert field in joined


def test_real_deploy_refuses_shape_valid_org_placeholder():
    msg = err(security={"org_id": "o-example1234"})
    assert "security.org_id is a placeholder" in msg
    assert "describe-organization" in msg


# ── the shipped presets ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "preset", ["migration.yaml", "federated.yaml", "production.yaml"]
)
def test_presets_with_placeholders_refuse_a_real_deploy(preset):
    # THE acceptance test: `deploy.sh deploy --profile migration` with the
    # preset untouched must stop here, not at sign-in.
    with pytest.raises(ValidationError) as e:
        load_platform_config(PRESETS / preset)
    assert "is a placeholder" in str(e.value)


@pytest.mark.parametrize("preset", sorted(p.name for p in PRESETS.glob("*.yaml")))
def test_every_preset_loads_for_the_parity_gate(preset, monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    load_platform_config(PRESETS / preset)


def test_federated_preset_splits_by_account(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    c = load_platform_config(PRESETS / "federated.yaml")
    platform = {s.rsplit("-", 1)[-1] for s in c.expected_stacks("000000000000")}
    workload = set(c.expected_stacks("123456789012"))
    assert "auth" in platform and "gateway" in platform
    assert not any(
        s.endswith("runtime-orchestrator") for s in c.expected_stacks("000000000000")
    )
    assert any(s.endswith("runtime-orchestrator") for s in workload)
    assert not any(s.endswith(("-auth", "-gateway")) for s in workload)


def test_distributed_preset_is_a_full_copy(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")
    c = load_platform_config(PRESETS / "distributed.yaml")
    assert c.deployment.strategy == "distributed"
    names = {s.split("-dev-", 1)[1] for s in c.expected_stacks("")}
    assert {
        "auth",
        "identity",
        "gateway",
        "memory",
        "runtime-orchestrator",
        "observability",
    } <= names


def test_federation_hand_over_values_are_checked():
    msg = err(
        deployment={
            "strategy": "federated",
            "platform_account": "222233334444",
            "workload_accounts": ["333344445555"],
            "federation": {
                "gateway_url": "https://REPLACE_ME.gateway.example/mcp",
                "issuer_url": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
                "m2m_client_id": "REPLACE_ME",
                "m2m_client_secret_name": "agentcore/platform-m2m",
            },
        }
    )
    assert "deployment.federation.gateway_url is a placeholder" in msg
    assert "deployment.federation.m2m_client_id is a placeholder" in msg
    assert "deploy.sh export" in msg
