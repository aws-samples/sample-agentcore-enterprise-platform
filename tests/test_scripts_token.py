"""Unit check for utils.get_m2m_token URL and Basic auth construction (no AWS needed)."""

import base64
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils

# Stub values, not credentials. Held in constants deliberately: a string literal
# compared against a variable named `token` reads as a hardcoded credential to
# Bandit (B105), and a HIGH finding that has to be explained away on every scan
# is worse than naming the fixture properly once.
STUB_RESPONSE_VALUE = "stub-value-from-fake-urlopen"
STUB_CLIENT_CONFIG_VALUE = "stub-value-from-fake-describe"


class FakeAwsClient:
    """Stands in for the ssm / cognito-idp / sts clients get_m2m_token creates."""

    def get_parameter(self, Name):
        value = "pool-123" if Name.endswith("user-pool-id") else "client-abc"
        return {"Parameter": {"Value": value}}

    def describe_user_pool_client(self, UserPoolId, ClientId):
        return {"UserPoolClient": {"ClientSecret": STUB_CLIENT_CONFIG_VALUE}}

    def get_caller_identity(self):
        return {"Account": "111122223333"}


def test_get_m2m_token_builds_url_and_basic_auth(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "myproj")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setattr(utils.boto3, "client", lambda *a, **kw: FakeAwsClient())

    captured = {}

    def fake_urlopen(req):
        captured["req"] = req
        return io.BytesIO(json.dumps({"access_token": STUB_RESPONSE_VALUE}).encode())

    monkeypatch.setattr(utils.urllib.request, "urlopen", fake_urlopen)

    token = utils.get_m2m_token()

    assert token == STUB_RESPONSE_VALUE
    req = captured["req"]
    assert (
        req.full_url
        == "https://myproj-prod-111122223333.auth.eu-west-1.amazoncognito.com/oauth2/token"
    )
    basic = f"client-abc:{STUB_CLIENT_CONFIG_VALUE}".encode()
    expected_auth = "Basic " + base64.b64encode(basic).decode()
    assert req.get_header("Authorization") == expected_auth
    assert b"grant_type=client_credentials" in req.data
    assert b"agentcore%2Finvoke" in req.data


def test_invoke_imports_without_aws_and_session_id_is_40_chars():
    """invoke.py must be importable with no AWS credentials (no module-level calls)."""
    import invoke

    sid = invoke.new_session_id()
    assert len(sid) == 40
    assert sid != invoke.new_session_id()
