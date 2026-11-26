"""JWT claim validation for agent identity.

Deliberately imports nothing beyond the standard library — no PyJWT, no
bedrock_agentcore — so the rules that decide whether a caller is who they claim
to be can be unit-tested anywhere (see tests/test_jwt_claims.py).

auth.py verifies the token's *signature* against the issuer's JWKS; this module
checks that the verified token is one we should accept: right issuer, one of our
clients, and an identity to act as.
"""

import time

# Asymmetric algorithms the supported IdPs (Cognito, Entra, Okta, Ping) sign
# with. Never 'none', never HS*: a symmetric alg would let anyone holding the
# public key forge a token.
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384"]

# Claims PyJWT must find before it will accept a token. Without exp a leaked
# token would be valid forever. iat is not relied on anywhere, so not required.
REQUIRED_CLAIMS = ["exp"]


class SuccessCache:
    """Cache the result of a successful build for ttl seconds.

    A build that raises is never cached, so a transient failure (e.g. OIDC
    discovery timing out) is retried on the next call instead of pinning a
    wrong value for the life of the process.
    """

    def __init__(self, ttl: float, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._value = None
        self._expires = 0.0

    def get(self, build):
        now = self._clock()
        if self._value is not None and now < self._expires:
            return self._value
        value = build()
        self._value = value
        self._expires = now + self.ttl
        return value


def gateway_token_scopes(env: dict | None = None) -> list[str]:
    """Scopes an agent requests for its Gateway M2M token.

    Empty (the Cognito default) means "whatever the client is assigned". An
    IdP issuing directly (Entra) refuses a client_credentials request with no
    scope, so app.py sets GATEWAY_TOKEN_SCOPES to the app's own `.default`.
    """
    import os

    raw = (env if env is not None else os.environ).get("GATEWAY_TOKEN_SCOPES", "")
    return [s for s in raw.split(",") if s]


def jwks_uri(issuer_url: str, discovery: dict | None = None) -> str:
    """Where the issuer publishes its signing keys.

    Cognito's is `<issuer>/.well-known/jwks.json`; Entra's is
    `.../discovery/v2.0/keys`, so the only honest source is the `jwks_uri`
    field of the OIDC discovery document. The Cognito shape is the fallback
    for callers that could not fetch discovery.
    """
    if discovery and discovery.get("jwks_uri"):
        return discovery["jwks_uri"]
    return f"{issuer_url.rstrip('/')}/.well-known/jwks.json"


class TokenRejected(ValueError):
    """The token was well-formed and signed but must not be trusted."""


def _client_ids(claims: dict) -> list:
    """The OAuth client(s) the token was issued to.

    Cognito puts it in `aud` for user-pool ID/access tokens obtained by a user,
    and in `client_id` for client_credentials (M2M) access tokens. Checking only
    `aud` silently accepts every M2M token in the account. When `aud` is a list
    every entry is a candidate, not just the first; `client_id` is consulted
    only when there is no audience.
    """
    audience = claims.get("aud")
    if isinstance(audience, str):
        audience = [audience]
    entries = [a for a in (audience or []) if a]
    if entries:
        return entries
    client_id = claims.get("client_id")
    return [client_id] if client_id else []


def validate_claims(
    claims: dict, expected_issuer: str, allowed_clients: tuple[str, ...] = ()
) -> str:
    """Return the subject of an acceptable token, or raise TokenRejected.

    allowed_clients empty means "any client from the expected issuer", which is
    the issuer's own trust boundary — narrower than accepting any issuer, wider
    than pinning to our clients. Callers that know their client IDs should pass
    them.
    """
    issuer = claims.get("iss")
    if not expected_issuer:
        raise TokenRejected(
            "No expected issuer configured, so no token can be validated. Set "
            "COGNITO_ISSUER_URL on the runtime (RuntimeStack passes it when the "
            "auth stack is deployed)."
        )
    if issuer != expected_issuer:
        raise TokenRejected(f"Token issuer {issuer!r} is not {expected_issuer!r}")

    if allowed_clients:
        client_ids = _client_ids(claims)
        if not any(c in allowed_clients for c in client_ids):
            raise TokenRejected(
                f"Token client {client_ids!r} is not one of this deployment's clients"
            )

    # token_use tells a Cognito access token from an ID token; both carry sub,
    # and either is fine for identifying the caller. Refresh tokens are not JWTs.
    subject = claims.get("sub")
    if not subject:
        raise TokenRejected(
            "Token has no 'sub' claim, so there is no identity to act as."
        )
    return subject
