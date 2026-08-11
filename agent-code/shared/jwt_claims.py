"""JWT claim validation for agent identity.

Deliberately imports nothing beyond the standard library — no PyJWT, no
bedrock_agentcore — so the rules that decide whether a caller is who they claim
to be can be unit-tested anywhere (see tests/test_jwt_claims.py).

auth.py verifies the token's *signature* against the issuer's JWKS; this module
checks that the verified token is one we should accept: right issuer, one of our
clients, and an identity to act as.
"""


class TokenRejected(ValueError):
    """The token was well-formed and signed but must not be trusted."""


def _client_id(claims: dict) -> str | None:
    """The OAuth client the token was issued to.

    Cognito puts it in `aud` for user-pool ID/access tokens obtained by a user,
    and in `client_id` for client_credentials (M2M) access tokens. Checking only
    `aud` silently accepts every M2M token in the account.
    """
    audience = claims.get("aud")
    if isinstance(audience, list):
        return audience[0] if audience else None
    return audience or claims.get("client_id")


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
        client_id = _client_id(claims)
        if client_id not in allowed_clients:
            raise TokenRejected(
                f"Token client {client_id!r} is not one of this deployment's clients"
            )

    # token_use tells a Cognito access token from an ID token; both carry sub,
    # and either is fine for identifying the caller. Refresh tokens are not JWTs.
    subject = claims.get("sub")
    if not subject:
        raise TokenRejected(
            "Token has no 'sub' claim, so there is no identity to act as."
        )
    return subject
