# The platform interface

What a use case (or any external consumer) may rely on. Everything here is
discoverable at runtime with only `ssm:GetParameter` and standard OAuth — no
imports of this repo's stacks, no CloudFormation exports, no IAM coupling on
the data plane. If it is not on this page, it is an internal and may change
without notice.

Interface version: **1**. Additions are non-breaking; renaming or removing a
parameter listed here is a breaking change and gets a version bump plus a
deprecation window.

## Discovery — the SSM namespace

All parameters live under `/{project}/{environment}/` (default:
`/agentcore-workshop/dev/`). Verified against a live deployment:

| Parameter | Meaning |
|---|---|
| `auth/issuer-url` | Cognito issuer; append `/.well-known/openid-configuration` for OIDC discovery |
| `auth/user-pool-id` | The user pool everything authenticates against |
| `auth/app-client-id` | Authorization-code + PKCE client (humans; has a secret, readable via `cognito-idp describe-user-pool-client`) |
| `auth/web-client-id` | Implicit-grant client (browser dashboards; no secret) |
| `auth/m2m-client-id` | client_credentials client (machines; scope `agentcore/invoke`) |
| `gateway/url` | MCP gateway endpoint — `tools/list` / `tools/call` with a Bearer JWT |
| `identity/gateway-credential-provider-name` | Token-vault provider agents use via `@requires_access_token` |
| `memory/memory-id`, `memory/memory-arn` | AgentCore Memory for this deployment |
| `runtimes/<component>/arn`, `runtimes/<component>/id` | Agent runtimes (`orchestrator`, plus `code-agent` / `research-agent` when A2A is on) |
| `identity/<vendor>-provider-arn` | 3LO credential providers, only when configured |
| `use-cases/<name>/...` | Each use case publishes its own outputs here |

In CDK, resolve at deploy time so your stack synthesizes without the platform
deployed:

```python
gateway_url = ssm.StringParameter.value_for_string_parameter(
    self, f"{ctx['ssm_prefix']}/gateway/url"
)
```

## Authentication — Cognito tokens, two shapes

- **Humans**: authorization-code + PKCE against the hosted UI using
  `auth/app-client-id`. With an enterprise IdP federated in (`IDP_TYPE=...`),
  the login happens at the IdP but the token is still MINTED BY COGNITO —
  your use case verifies one issuer regardless of the customer's IdP
  (docs/ENTERPRISE_IDP.md).
- **Machines**: client_credentials with `auth/m2m-client-id` against the
  Cognito domain's `/oauth2/token` (scripts/utils.py `get_m2m_token` is the
  reference).

Both produce a Bearer JWT accepted by the gateway (CUSTOM_JWT authorizer) and
the HTTP runtimes' `/invocations` endpoint. Two caveats that cost real
debugging time: the runtime authorizer validates signature, issuer, and
client id — **not scopes** (enforce scopes in your handler if you need them);
and A2A runtimes are the exception to everything above — SigV4 only, no
Bearer.

## Federation

The interface is identical in a federated deployment; only WHERE things live
changes. A workload account has its own memory/runtimes/identity parameters
but no local `auth/*` or `gateway/*` — those values come from the platform
account via the `deployment.federation` block in `platform.yaml`. This is why
a use case declares `requires:` in its manifest: the contract refuses to
enable it on a side of the federation that lacks its prerequisites.

## What is deliberately NOT interface

Core stack class names, construct trees, CloudFormation outputs/exports,
resource names not published to SSM, and the internals of `deploy.sh`. Use
cases that reach past SSM into core stacks will break without warning, and
review will reject them anyway (CONTRIBUTING_USE_CASES.md).
