"""identity.mode defaults per IdP when omitted; an explicit mode is honoured."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from infra_utils import platform_config as pc
from infra_utils.platform_config import IdentityConfig, identity_mode_warnings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_mode_env(monkeypatch):
    monkeypatch.delenv("IDP_MODE", raising=False)
    monkeypatch.setattr(pc, "_EMITTED_MODE_WARNINGS", set())


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


def test_loading_config_prints_switch_warning(capsys):
    IdentityConfig.model_validate(dict(_ENTRA))
    err = capsys.readouterr().err
    assert "[WARN] identity.mode is not set" in err
    assert "identity.mode: brokered" in err


def test_pinned_brokered_load_prints_nothing(capsys):
    IdentityConfig.model_validate({**_ENTRA, "mode": "brokered"})
    assert "identity.mode is not set" not in capsys.readouterr().err


@pytest.mark.parametrize("override", ["brokered", "direct"])
def test_explicit_env_mode_override_does_not_warn(monkeypatch, capsys, override):
    monkeypatch.setenv("IDP_MODE", override)
    identity = IdentityConfig.model_validate(dict(_ENTRA))
    assert identity_mode_warnings(identity) == []
    assert "identity.mode is not set" not in capsys.readouterr().err


def _run_validate(env_extra=None):
    manifest = {
        "deployment": {"strategy": "centralized", "platform_account": "111111111111"},
        "identity": dict(_ENTRA),
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("IDP_MODE", "PLATFORM_ALLOW_PLACEHOLDERS")
    }
    env.update(env_extra or {})
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "platform.yaml"
        path.write_text(yaml.safe_dump(manifest))
        return subprocess.run(
            [sys.executable, "-m", "infra_utils.platform_config", str(path)],
            check=False,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


def test_validate_command_displays_switch_warning():
    result = _run_validate()
    assert "identity.mode is not set" in result.stderr
    assert "identity.mode: brokered" in result.stderr


def test_validate_command_respects_env_mode_override():
    result = _run_validate({"IDP_MODE": "brokered"})
    assert "identity.mode is not set" not in result.stderr


@pytest.mark.parametrize("preset", ["production", "federated"])
def test_enterprise_presets_pin_direct(preset):
    doc = yaml.safe_load((ROOT / "presets" / f"{preset}.yaml").read_text())
    assert doc["identity"]["mode"] == "direct"
