"""JWT claim validation for agent identity.

Deliberately imports nothing beyond the standard library — no PyJWT, no
bedrock_agentcore — so the rules that decide whether a caller is who they claim
to be can be unit-tested anywhere (see tests/test_jwt_claims.py).

auth.py verifies the token's *signature* against the issuer's JWKS; this module
checks that the verified token is one we should accept: right issuer, one of our
clients, and an identity to act as.
"""

import re


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


# Long-term memory namespaces, one set per actor. Must match the templates in
# stacks/memory_stack.py (tests/test_memory_namespaces.py checks they agree).
LTM_PREFERENCES_NAMESPACE = "/users/{actorId}/preferences"
LTM_FACTS_NAMESPACE = "/users/{actorId}/facts"
DEFAULT_LTM_TOP_K = 10
DEFAULT_LTM_RELEVANCE_SCORE = 0.3

# Namespace-safe actor ids: no '/', braces, or whitespace, so an actor id can
# never address another actor's namespace or a parent prefix.
_SAFE_ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,254}")


def build_retrieval_namespaces(actor_id: str, include_facts: bool = True) -> list[str]:
    """Resolve the per-actor namespace templates for one verified actor.

    actor_id must be the verified JWT sub (validate_claims), never a value
    from the request body.
    """
    if not isinstance(actor_id, str) or not _SAFE_ACTOR_ID.fullmatch(actor_id):
        raise ValueError("Actor id is empty or not namespace-safe")
    templates = [LTM_PREFERENCES_NAMESPACE]
    if include_facts:
        templates.append(LTM_FACTS_NAMESPACE)
    return [template.replace("{actorId}", actor_id) for template in templates]


def ltm_retrieval_settings(env: dict | None = None) -> tuple[int, float]:
    """(top_k, relevance_score) from LTM_TOP_K / LTM_RELEVANCE_SCORE."""
    import os

    source = env if env is not None else os.environ
    raw_top_k = source.get("LTM_TOP_K") or str(DEFAULT_LTM_TOP_K)
    raw_score = source.get("LTM_RELEVANCE_SCORE") or str(DEFAULT_LTM_RELEVANCE_SCORE)
    try:
        top_k = int(raw_top_k)
        relevance_score = float(raw_score)
    except ValueError as exc:
        raise ValueError(
            "LTM_TOP_K must be an integer and LTM_RELEVANCE_SCORE a number"
        ) from exc
    if top_k < 1:
        raise ValueError("LTM_TOP_K must be at least 1")
    if not 0.0 <= relevance_score <= 1.0:
        raise ValueError("LTM_RELEVANCE_SCORE must be between 0 and 1")
    return top_k, relevance_score


def memory_retrieval_plan(actor_id: str, env: dict | None = None) -> dict[str, dict]:
    """Retrieval settings keyed by the actor's own namespaces.

    The facts namespace is only included when USE_LONG_TERM_MEMORY is true,
    because the semantic strategy only exists then.
    """
    import os

    source = env if env is not None else os.environ
    top_k, relevance_score = ltm_retrieval_settings(source)
    include_facts = source.get("USE_LONG_TERM_MEMORY", "false").lower() == "true"
    return {
        namespace: {"top_k": top_k, "relevance_score": relevance_score}
        for namespace in build_retrieval_namespaces(actor_id, include_facts)
    }
