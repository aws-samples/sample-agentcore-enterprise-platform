"""App JWT + installation token minting with a throwaway key and mocked HTTP. Never calls GitHub."""

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from factory import github

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_app_jwt_is_rs256_signed_and_short_lived():
    token = github.app_jwt("12345", PEM, now=1_000_000)
    header, claims, sig = token.split(".")
    assert json.loads(b64d(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(b64d(claims)) == {
        "iat": 999_940,
        "exp": 1_000_540,
        "iss": "12345",
    }
    KEY.public_key().verify(
        b64d(sig), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_installation_token_is_down_scoped_to_the_repo(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        if (
            req.get_method() == "GET"
        ):  # this installation lacks workflows:write and issues:write
            granted = {
                "contents": "write",
                "pull_requests": "write",
                "checks": "write",
                "actions": "read",
            }
            return FakeResponse(json.dumps({"permissions": granted}).encode())
        return FakeResponse(b'{"token": "ghs_fake"}')

    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen)
    creds = {"app_id": "12345", "installation_id": "678", "private_key_pem": PEM}
    token, missing = github.installation_token(
        creds, "aws-samples/sample-agentcore-enterprise-platform"
    )
    assert token == "ghs_fake" and missing == ["issues:write", "workflows:write"]
    get, post = seen
    assert get.full_url == "https://api.github.com/app/installations/678"
    assert post.full_url == "https://api.github.com/app/installations/678/access_tokens"
    body = json.loads(post.data)
    assert body["repositories"] == ["sample-agentcore-enterprise-platform"]
    assert body["permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "actions": "read",
    }
    # The bearer is unredirected, so a redirect (job logs -> blob storage) never carries it.
    assert (
        post.unredirected_hdrs["Authorization"].startswith("Bearer ey")
        and "Authorization" not in post.headers
    )


def test_existing_pr_is_reused(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.get_method())
        return FakeResponse(b'[{"html_url": "https://github.com/o/r/pull/7"}]')

    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen)
    assert (
        github.open_pull_request("o/r", "t", "factory/ci-fix/1", "main", "t", "b")
        == "https://github.com/o/r/pull/7"
    )
    assert calls == ["GET"]
