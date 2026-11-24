"""IdentityConfig: Okta as a direct issuer."""

import pytest
from pydantic import ValidationError

from infra_utils.platform_config import IdentityConfig, PlatformConfig, to_env

OKTA = {
    "idp": "okta",
    "mode": "direct",
    "issuer_url": "https://acme.okta.com/oauth2/default",
    "client_id": "0oa1abcdefg",
    "client_secret_name": "agentcore/okta-client-secret",
    "audience": "api://agentcore",
    "m2m_scope": "agentcore.invoke",
}


@pytest.fixture(autouse=True)
def _no_placeholder_bypass(monkeypatch):
    monkeypatch.delenv("PLATFORM_ALLOW_PLACEHOLDERS", raising=False)


def test_okta_direct_accepted():
    ident = IdentityConfig(**OKTA)
    assert ident.direct_issuer_url == "https://acme.okta.com/oauth2/default"
    assert ident.direct_audience == "api://agentcore"
    assert ident.direct_m2m_scope == "agentcore.invoke"


def test_okta_direct_custom_auth_server_id_accepted():
    ident = IdentityConfig(
        **{**OKTA, "issuer_url": "https://acme.okta.com/oauth2/aus1abc/"}
    )
    assert ident.direct_issuer_url == "https://acme.okta.com/oauth2/aus1abc"


def test_okta_direct_refuses_org_authorization_server():
    with pytest.raises(ValidationError, match="cannot be validated by third parties"):
        IdentityConfig(**{**OKTA, "issuer_url": "https://acme.okta.com"})


def test_okta_direct_requires_issuer():
    with pytest.raises(ValidationError, match="issuer_url"):
        IdentityConfig(**{**OKTA, "issuer_url": ""})


@pytest.mark.parametrize("field", ["audience", "m2m_scope"])
def test_okta_direct_requires_field(field):
    with pytest.raises(ValidationError, match=f"identity.{field}"):
        IdentityConfig(**{**OKTA, field: ""})


def test_okta_direct_placeholder_audience_refused():
    with pytest.raises(ValidationError, match="identity.audience is a placeholder"):
        IdentityConfig(**{**OKTA, "audience": "REPLACE_ME"})


def test_okta_brokered_unaffected():
    ident = IdentityConfig(
        idp="okta",
        issuer_url="https://acme.okta.com",
        client_id="0oa1abcdefg",
        client_secret_name="agentcore/okta-client-secret",
    )
    assert ident.mode == "brokered"


def test_entra_direct_unchanged():
    ident = IdentityConfig(
        idp="entra_id",
        mode="direct",
        tenant_id="3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        client_id="9a8b7c6d-1111-2222-3333-444455556666",
        client_secret_name="agentcore/idp-client-secret",
    )
    assert ident.direct_m2m_scope == "9a8b7c6d-1111-2222-3333-444455556666/.default"
    assert ident.direct_audience == "9a8b7c6d-1111-2222-3333-444455556666"


def test_to_env_exports_okta_audience_and_scope():
    env = to_env(
        PlatformConfig.model_validate(
            {"deployment": {"platform_account": "111122223333"}, "identity": OKTA}
        )
    )
    assert env["IDP_AUDIENCE"] == "api://agentcore"
    assert env["IDP_M2M_SCOPE"] == "agentcore.invoke"
    assert env["IDP_MODE"] == "direct"


def test_to_env_omits_unset_audience_and_scope():
    env = to_env(PlatformConfig())
    assert "IDP_AUDIENCE" not in env
    assert "IDP_M2M_SCOPE" not in env


def test_ping_direct_still_refused():
    with pytest.raises(ValidationError, match="deploys brokered"):
        IdentityConfig(
            idp="ping",
            mode="direct",
            issuer_url="https://auth.pingone.com/abc/as",
            client_id="pingclient",
            client_secret_name="agentcore/ping-client-secret",
        )
