# Verify Caller Identity

Agents identify callers from the JWT in the `Authorization` header, not the request body.
This document explains how that check works, why the agent validates the token even though
AgentCore already does, and which parts of the deployment depend on it.

## How the agent checks tokens

`agent-code/shared/auth.py` (`extract_user_id_from_context`) verifies the token signature
against the issuer's published JWKS, pinned to `RS256`, then hands the claims to
`validate_claims` in `agent-code/shared/jwt_claims.py`, which checks `iss`, the client, and
the presence of `sub`. Identity is the `sub` claim.

Every failure path raises — there is no fallback to an unverified decode:

| Condition | Result |
|---|---|
| No `Authorization` header (or no request headers) | reject |
| `COGNITO_ISSUER_URL` unset | reject, *before* decoding |
| Bad signature, expired, unknown `kid`, JWKS unreachable | reject |
| `iss` mismatch | reject |
| Client not in `COGNITO_ALLOWED_CLIENTS` | reject |
| `COGNITO_ALLOWED_CLIENTS` **empty** | accepted — issuer-only pinning |
| No `sub` | reject |

The last two rows are the asymmetry worth knowing: a missing issuer is a hard reject, but a
missing client allowlist degrades quietly to "any client of the correct issuer." Set both.

`token_use` is deliberately not checked, so both Cognito access tokens and ID tokens are
accepted as identity.

## Why check the token twice?

AgentCore Runtime's `CUSTOM_JWT` authorizer validates a token before the container sees it.
The agent validates it again so the check travels with your code rather than depending on how
the runtime happens to be deployed.

`runtime_stack.py` attaches an authorizer only when the protocol is client-facing
(`HTTP`, `MCP`, `AGUI` — see `infra_utils/runtime_protocol.py`) **and** a Cognito issuer was
supplied. The `Authorization` header allowlist is gated on exactly the same condition, because
the control plane enforces it: creating a runtime that allowlists `Authorization` without a
`customJWTAuthorizer` is rejected with a validation error (observed live on an A2A deploy).
So an A2A runtime, or any runtime built without an issuer, never sees an `Authorization`
header at all — its agent cannot require a caller JWT, and such runtimes are guarded by IAM
(`InvokeAgentRuntime`) instead. The agent-side verification in `shared/auth.py` still stands
on client-facing runtimes: it keeps the check in your code rather than depending on how the
runtime happens to be deployed.

The Gateway has its own separate `CUSTOM_JWT` authorizer, configured the same way in
`stacks/gateway_stack.py`. Runtime and Gateway are independent — configuring one says nothing
about the other.

## Environment variables

`COGNITO_ISSUER_URL` and `COGNITO_ALLOWED_CLIENTS` are injected by **`app.py`**, not by
`RuntimeStack` itself, and today only for the orchestrator runtime:

```python
"COGNITO_ISSUER_URL": auth_stack.issuer_url,
"COGNITO_ALLOWED_CLIENTS": f"{auth_stack.app_client_id},{auth_stack.m2m_client_id}",
```

`RuntimeStack`'s own `env_vars` carry only `PROJECT_NAME`, `ENVIRONMENT`, `COMPONENT_NAME`,
`AWS_REGION_NAME` and `SOURCE_HASH`; its `cognito_issuer_url` / `cognito_allowed_clients`
parameters feed the authorizer, not the container. The A2A runtimes (`code-agent`,
`research-agent`) receive neither variable. That is harmless only because their entrypoints
take no `RequestContext` and never call the helper — **any agent added there that does call it
will fail closed until `app.py` passes the issuer through.**

JWKS is fetched on the first verified request and cached by `PyJWKClient`, which refreshes on
a 5-minute lifespan and re-fetches when it sees an unknown key id, so Cognito key rotation is
handled. No explicit timeout is set, so a hung JWKS endpoint stalls the request for PyJWT's
30-second default before failing closed.

## Machine-to-machine callers

Cognito M2M (`client_credentials`) access tokens carry `client_id` instead of `aud`. Checking
only `aud` would reject machine callers, including `scripts/invoke.py`. `jwt_claims.py` prefers
`aud` when present (taking the first element of a list) and falls back to `client_id`; this is
why `auth.py` passes `verify_aud: False` and validates the audience itself. `app.py` includes
the M2M client id in `COGNITO_ALLOWED_CLIENTS` so those callers pass the allowlist.

## What the verified identity is used for

The verified `sub` becomes the AgentCore Memory `actor_id`, which is the tenant boundary for
stored conversation history. An unverified or defaulted identity would place different callers
under one actor and mix their history, so agents reject rather than substitute a placeholder.

## Which agents verify

Call `extract_user_id_from_context`:
`strands-agent`, `agui-strands-agent`, `langgraph-agent`, `agui-langgraph-agent`,
`claude-sdk-agent`, `claude-sdk-multi-agent`.

Extract no identity at all — their entrypoints take no `RequestContext`:
`orchestrator`, `code-agent`, `research-agent`. Note `app.py` defaults `agent_pattern` to
`orchestrator`. If you add identity-dependent behaviour to any of these, add the
`RequestContext` parameter and the helper call, and make sure the runtime is passed an issuer.

Agents that import `shared/` must also `COPY shared/ shared/` in their Dockerfile.

## Outbound auth (agent → Gateway)

Separate mechanism, same file: `get_gateway_access_token()` uses
`@requires_access_token(provider_name=os.environ["GATEWAY_CREDENTIAL_PROVIDER_NAME"], auth_flow="M2M")`.
The decorator is evaluated at import time and `provider_name` defaults to `""`, so a runtime
without that variable binds an empty provider name at module load. The A2A runtimes
intentionally do not receive it.

## Testing note

`scripts/test.py` sends the bearer token as `X-Authorization` when invoking through boto3. The
runtime allowlist forwards only `Authorization`, so an agent calling the helper on that path
raises "No Authorization header found." Use `scripts/invoke.py` to exercise real JWT auth.
