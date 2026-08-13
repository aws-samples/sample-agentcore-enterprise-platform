# Verify Caller Identity

Agents identify callers from the JWT in the `Authorization` header, not the request body. This document explains how that check works and why the agent validates the token even though AgentCore already does.

## How the agent checks tokens

`agent-code/shared/auth.py` checks the token signature against the issuer's JWKS. It also checks expiry, issuer, and whether the client belongs to this deployment. If a check fails, the request fails.

## Why check the token twice?

AgentCore Runtime's `CUSTOM_JWT` authorizer validates a token before the container sees it. The agent validates it again so the check stays with your code even if the runtime authorizer changes. `runtime_stack.py` adds an authorizer only for client-facing protocols with a Cognito issuer. An A2A runtime, or any runtime without an issuer, passes through the `Authorization` header it receives. The agent should still verify it.

## Environment variables

`RuntimeStack` provides `COGNITO_ISSUER_URL` and `COGNITO_ALLOWED_CLIENTS`. Without them, the agent rejects the request. JWKS is fetched once per container and cached, so this adds one HTTPS call at cold start.

## Machine-to-machine callers

Cognito M2M (`client_credentials`) access tokens use `client_id` instead of `aud`. Checking only `aud` would reject machine callers, including `scripts/invoke.py`. See `agent-code/shared/jwt_claims.py`.
