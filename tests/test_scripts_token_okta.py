"""scripts/utils.py: the client_credentials request for Okta, no network."""

import importlib.util
import json
import urllib.parse
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "utils.py"
_spec = importlib.util.spec_from_file_location("scripts_utils_okta", _PATH)
utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(utils)

ISSUER = "https://acme.okta.com/oauth2/default"


def test_build_okta_client_credentials_request():
    req = utils.build_client_credentials_request(
        f"{ISSUER}/v1/token", "0oa1", "s3cret", "agentcore.invoke"
    )
    assert req["method"] == "POST"
    assert req["url"] == f"{ISSUER}/v1/token"
    assert req["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert req["body"] == {
        "grant_type": "client_credentials",
        "client_id": "0oa1",
        "client_secret": "s3cret",
        "scope": "agentcore.invoke",
    }


def test_build_request_refuses_plain_http():
    with pytest.raises(ValueError, match="https"):
        utils.build_client_credentials_request("http://x/token", "a", "b", "c")


def test_direct_token_uses_discovery_endpoint_and_published_scope(monkeypatch):
    params = {
        "auth/issuer-url": ISSUER,
        "auth/m2m-client-id": "0oa1",
        "auth/m2m-scope": "agentcore.invoke",
        "auth/m2m-client-secret-name": "agentcore/okta-client-secret",
    }
    monkeypatch.setattr(utils, "get_ssm_param", lambda name, p, e: params[name])
    monkeypatch.setattr(
        utils, "oidc_discovery", lambda issuer: {"token_endpoint": f"{issuer}/v1/token"}
    )

    class _Secrets:
        def get_secret_value(self, SecretId):
            assert SecretId == "agentcore/okta-client-secret"
            return {"SecretString": "s3cret"}

    monkeypatch.setattr(utils.boto3, "client", lambda *a, **k: _Secrets())
    sent = {}

    class _Resp:
        def read(self):
            return json.dumps({"access_token": "tok"}).encode()

    def _urlopen(req):
        sent["url"] = req.full_url
        sent["method"] = req.get_method()
        sent["body"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(utils.urllib.request, "urlopen", _urlopen)
    assert utils._get_direct_m2m_token("p", "dev", "us-east-1") == "tok"
    assert sent["url"] == f"{ISSUER}/v1/token"
    assert sent["method"] == "POST"
    assert sent["body"]["scope"] == "agentcore.invoke"
    assert sent["body"]["grant_type"] == "client_credentials"
