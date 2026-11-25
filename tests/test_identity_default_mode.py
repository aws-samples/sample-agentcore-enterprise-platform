"""identity.mode defaults per IdP when omitted; an explicit mode is honoured."""

from pathlib import Path

import pytest
import yaml

from infra_utils.platform_config import IdentityConfig, identity_mode_warnings

ROOT = Path(__file__).resolve().parents[1]

_ENTRA = {
    "idp": "entra_id",
    "tenant_id": "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "client_id": "9a8b7c6d-1234-4abc-9def-000111222333",
    "client_secret_name": "agentcore/idp-client-secret",  # pragma: allowlist secret
}


def _issuer(idp: str) -> dict:
    return {
        "idp": idp,
        "issuer_url": "https://corp.okta.com/oauth2/default",
        "client_id": "0oa1b2c3d4",
        "client_secret_name": "agentcore/idp-client-secret",  # pragma: allowlist secret
    }


IDPS = {
    "cognito": ({"idp": "cognito"}, "brokered"),
    "entra_id": (_ENTRA, "direct"),
    # Okta direct has not landed: okta keeps brokered until it does.
    "okta": (_issuer("okta"), "brokered"),
    "ping": (_issuer("ping"), "brokered"),
}


@pytest.mark.parametrize("idp", sorted(IDPS))
def test_default_mode_per_idp(idp):
    fields, expected = IDPS[idp]
    assert IdentityConfig.model_validate(dict(fields)).mode == expected


@pytest.mark.parametrize("fields", [_ENTRA, _issuer("okta")], ids=["entra", "okta"])
def test_explicit_brokered_preserved_without_warning(fields):
    identity = IdentityConfig.model_validate({**fields, "mode": "brokered"})
    assert identity.mode == "brokered"
    assert identity_mode_warnings(identity) == []


def test_explicit_direct_does_not_warn():
    identity = IdentityConfig.model_validate({**_ENTRA, "mode": "direct"})
    assert identity.mode == "direct"
    assert identity_mode_warnings(identity) == []


def test_omitted_mode_with_entra_warns_about_switch():
    identity = IdentityConfig.model_validate(dict(_ENTRA))
    (warning,) = identity_mode_warnings(identity)
    assert "identity.mode is not set" in warning
    assert "'direct'" in warning and "entra_id" in warning
    assert "mode: brokered" in warning


@pytest.mark.parametrize("idp", ["okta", "cognito", "ping"])
def test_omitted_mode_resolving_brokered_does_not_warn(idp):
    identity = IdentityConfig.model_validate(dict(IDPS[idp][0]))
    assert identity_mode_warnings(identity) == []


@pytest.mark.parametrize("preset", ["production", "federated"])
def test_enterprise_presets_pin_direct(preset):
    doc = yaml.safe_load((ROOT / "presets" / f"{preset}.yaml").read_text())
    assert doc["identity"]["mode"] == "direct"
