"""A verified signature is not enough: the claims decide who the caller is.

Background: shared/auth.py used to decode the caller's JWT with
verify_signature=False, on the argument that AgentCore Runtime had already
validated it. True for the runtimes in this repo today, but it is a property of
the deployment rather than of the code — a runtime without an authorizer (A2A,
or any runtime built without a Cognito issuer) forwards whatever Authorization
header it receives. A security scan flagged the unverified decode as HIGH, and
it was right to.
"""

import sys
from pathlib import Path

import pytest

# Imported as a bare module, not via the shared package: shared/__init__.py
# pulls in auth.py and therefore PyJWT + bedrock_agentcore, which the CI test
# job does not install. jwt_claims itself imports only the standard library,
# which is the whole point of keeping the claim rules in their own module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-code" / "shared"))

from jwt_claims import TokenRejected, validate_claims

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
APP_CLIENT = "app-client-id"
M2M_CLIENT = "m2m-client-id"
CLIENTS = (APP_CLIENT, M2M_CLIENT)


def test_user_token_is_accepted():
    claims = {"iss": ISSUER, "aud": APP_CLIENT, "sub": "user-1"}
    assert validate_claims(claims, ISSUER, CLIENTS) == "user-1"


def test_m2m_token_is_accepted_via_client_id():
    """Cognito client_credentials tokens carry client_id, not aud.

    Checking only aud would reject every M2M caller — which is how the workshop's
    own scripts authenticate.
    """
    claims = {"iss": ISSUER, "client_id": M2M_CLIENT, "sub": M2M_CLIENT}
    assert validate_claims(claims, ISSUER, CLIENTS) == M2M_CLIENT


def test_audience_list_is_supported():
    claims = {"iss": ISSUER, "aud": [APP_CLIENT], "sub": "user-1"}
    assert validate_claims(claims, ISSUER, CLIENTS) == "user-1"


def test_token_from_another_issuer_is_rejected():
    """The attack this guards: a valid token from someone else's user pool."""
    claims = {"iss": "https://evil.example.com", "aud": APP_CLIENT, "sub": "user-1"}
    with pytest.raises(TokenRejected, match="issuer"):
        validate_claims(claims, ISSUER, CLIENTS)


def test_token_from_an_unknown_client_is_rejected():
    """A token from the right pool but a client we did not authorize."""
    claims = {"iss": ISSUER, "client_id": "some-other-app", "sub": "user-1"}
    with pytest.raises(TokenRejected, match="client"):
        validate_claims(claims, ISSUER, CLIENTS)


def test_missing_expected_issuer_rejects_everything():
    """Unconfigured must mean closed, never 'accept anything'."""
    claims = {"iss": ISSUER, "aud": APP_CLIENT, "sub": "user-1"}
    with pytest.raises(TokenRejected, match="No expected issuer"):
        validate_claims(claims, "", CLIENTS)


def test_token_without_subject_is_rejected():
    claims = {"iss": ISSUER, "aud": APP_CLIENT}
    with pytest.raises(TokenRejected, match="sub"):
        validate_claims(claims, ISSUER, CLIENTS)


def test_no_allowed_clients_still_pins_the_issuer():
    """Empty allowlist widens to 'any client of our pool', not 'any token'."""
    claims = {"iss": ISSUER, "client_id": "anything", "sub": "user-1"}
    assert validate_claims(claims, ISSUER, ()) == "user-1"
    with pytest.raises(TokenRejected, match="issuer"):
        validate_claims({**claims, "iss": "https://evil.example.com"}, ISSUER, ())


def test_empty_claims_are_rejected():
    with pytest.raises(TokenRejected):
        validate_claims({}, ISSUER, CLIENTS)
