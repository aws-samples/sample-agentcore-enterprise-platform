"""The one place that renders an AgentCore custom JWT authorizer.

Gateway and Runtime take the same CustomJWTAuthorizerConfiguration; keeping the
shape here means the two can never disagree on how "which tokens do we accept"
is expressed.

AgentCore matches `allowedClients` against the token's `client_id` claim and
`allowedAudience` against `aud` (developer guide, inbound-jwt-authorizer).
Cognito M2M tokens carry `client_id`; Entra tokens carry `aud`/`azp` and no
`client_id` at all, so a direct-mode deployment must pin the audience or the
authorizer would trust every token from the tenant. Both lists have a minimum
length of 1 in the CloudFormation schema, so an empty one is omitted rather
than sent.
"""

from __future__ import annotations


def custom_jwt_authorizer(
    issuer_url: str,
    allowed_clients: list[str] | None = None,
    allowed_audience: list[str] | None = None,
) -> dict:
    if not (allowed_clients or allowed_audience):
        raise ValueError(
            "a JWT authorizer needs allowed_clients or allowed_audience; with "
            "neither, every token from the issuer would be accepted"
        )
    cfg: dict = {
        "discoveryUrl": f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    }
    if allowed_clients:
        cfg["allowedClients"] = list(allowed_clients)
    if allowed_audience:
        cfg["allowedAudience"] = list(allowed_audience)
    return {"customJwtAuthorizer": cfg}
