# Adapted from fullstack-solution-template-for-agentcore
"""
Authentication utilities for agent patterns.

Provides secure user identity extraction from JWT tokens in the AgentCore Runtime
RequestContext (prevents impersonation via prompt injection).
"""

import logging
import os

import jwt
from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.runtime import RequestContext

from shared.jwt_claims import TokenRejected, validate_claims

logger = logging.getLogger(__name__)

# Issuer and clients this deployment accepts. RuntimeStack injects both.
COGNITO_ISSUER_URL = os.environ.get("COGNITO_ISSUER_URL", "").rstrip("/")
ALLOWED_CLIENTS = tuple(
    c for c in os.environ.get("COGNITO_ALLOWED_CLIENTS", "").split(",") if c
)

# JWKS is fetched once per container and cached by PyJWKClient, which also
# refreshes when it sees an unknown key id (Cognito rotates signing keys).
_jwks_client: jwt.PyJWKClient | None = None


def _signing_key(token: str) -> str:
    """Public key for this token, from the issuer's published JWKS."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{COGNITO_ISSUER_URL}/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client.get_signing_key_from_jwt(token).key


def extract_user_id_from_context(context: RequestContext) -> str:
    """
    Extract the caller's user ID from the verified JWT in the request context.

    The token's signature, expiry, issuer and client are all checked here even
    though AgentCore Runtime's CUSTOM_JWT authorizer already validated the token
    upstream. That is deliberate defence in depth: the upstream check is a
    property of the *deployment*, not of this code. A runtime deployed without
    an authorizer — an A2A runtime, or any runtime built without a Cognito
    issuer — forwards whatever Authorization header it is handed, and an
    unverified decode would then take an attacker's word for who they are.

    The identity comes from the token's 'sub' claim, never from the request
    payload, so it cannot be moved by prompt injection.

    Args:
        context (RequestContext): The request context provided by AgentCore
            Runtime, containing the forwarded Authorization header.

    Returns:
        str: The user ID (sub claim) of the verified token.

    Raises:
        ValueError: If the Authorization header is missing, or the token fails
            verification, or it carries no usable identity. Never falls back to
            trusting an unverified token.
    """
    request_headers = context.request_headers
    if not request_headers:
        raise ValueError(
            "No request headers found in context. "
            "Ensure the AgentCore Runtime is configured with a request header allowlist "
            "that includes the Authorization header."
        )

    auth_header = request_headers.get("Authorization")
    if not auth_header:
        raise ValueError(
            "No Authorization header found in request context. "
            "Ensure the AgentCore Runtime is configured with JWT inbound auth "
            "and the Authorization header is in the request header allowlist."
        )

    # Remove "Bearer " prefix to get the raw JWT token
    token = (
        auth_header.replace("Bearer ", "")
        if auth_header.startswith("Bearer ")
        else auth_header
    )

    if not COGNITO_ISSUER_URL:
        raise ValueError(
            "COGNITO_ISSUER_URL is not set, so the token's signature cannot be "
            "verified. Refusing to trust an unverified token — redeploy the "
            "runtime so RuntimeStack injects the issuer."
        )

    try:
        # Signature and expiry are verified against the issuer's JWKS. aud is
        # checked in validate_claims instead: Cognito M2M access tokens carry
        # client_id rather than aud, and verify_aud here would reject them.
        claims = jwt.decode(
            jwt=token,
            key=_signing_key(token),
            algorithms=["RS256"],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": False,
            },
        )
    except jwt.PyJWTError as exc:
        # Message only: the token itself must never reach the logs.
        raise ValueError(f"JWT verification failed: {exc}") from exc

    try:
        user_id = validate_claims(claims, COGNITO_ISSUER_URL, ALLOWED_CLIENTS)
    except TokenRejected as exc:
        raise ValueError(str(exc)) from exc

    logger.info("Verified JWT for user_id: %s", user_id)
    return user_id


@requires_access_token(
    provider_name=os.environ.get("GATEWAY_CREDENTIAL_PROVIDER_NAME", ""),
    auth_flow="M2M",
    scopes=[],
)
def get_gateway_access_token(access_token: str) -> str:
    """
    Fetch OAuth2 access token for AgentCore Gateway authentication.

    The @requires_access_token decorator handles token retrieval and refresh:
    1. Token Retrieval: Calls GetResourceOauth2Token API to fetch token from Token Vault
    2. Automatic Refresh: Uses refresh tokens to renew expired access tokens
    3. Error Orchestration: Handles missing tokens and OAuth flow management

    For M2M (Machine-to-Machine) flows, the decorator uses Client Credentials grant type.
    The provider_name must match the Name field in the CDK OAuth2CredentialProvider resource.

    This is synchronous because it's called during agent setup before the async
    message processing loop.
    """
    return access_token
