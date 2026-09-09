<!-- markdownlint-disable MD041 -->

# Identity module

The identity module (`stacks/identity_stack.py`) creates the platform's
AgentCore OAuth2 credential providers — the entries in the AgentCore Identity
token vault that agents draw tokens from at runtime via
`@requires_access_token`. One provider is always created: the gateway M2M
provider, which exchanges the Cognito M2M client credentials
(`client_credentials` grant) for gateway access tokens, so agents never hold
the client secret themselves. Three more are conditional: 3LO (three-legged
OAuth) providers for Google, GitHub, and Notion, letting agents act on a
user's behalf against those services.

## When it deploys

Always — in every configuration and on **both** sides of a federation.
`expected_stacks()` in `infra_utils/platform_config.py` appends
`{prefix}-identity` unconditionally. Token vaults are account-local, so a
federated workload account needs its own provider holding the *platform*
account's M2M credentials, even though the issuer lives in the platform
account ([Multi-account federation](../MULTI_ACCOUNT.md)).

## What it creates

| Resource | Name pattern | Purpose |
|---|---|---|
| `AWS::BedrockAgentCore::OAuth2CredentialProvider` | `{prefix}-gateway-m2m` | Always. `CustomOauth2` vendor pointed at the Cognito OIDC discovery URL; the token vault performs the `client_credentials` exchange |
| `AWS::BedrockAgentCore::OAuth2CredentialProvider` (optional) | `{prefix}-google-oauth` | 3LO, `GoogleOauth2` vendor — only when `google_client_id` is set |
| `AWS::BedrockAgentCore::OAuth2CredentialProvider` (optional) | `{prefix}-github-oauth` | 3LO, `GithubOauth2` vendor — only when `github_client_id` is set |
| `AWS::BedrockAgentCore::OAuth2CredentialProvider` (optional) | `{prefix}-notion-oauth` | 3LO, `CustomOauth2` with explicit endpoints (Notion publishes no OIDC discovery document) — only when `notion_client_id` is set |
| `AWS::SSM::Parameter` | `/{project}/{environment}/identity/*` | Provider name / ARNs (see Interfaces) |

No Lambdas, no custom resources — the stack is pure L1 AgentCore resources
plus SSM parameters.

## Configuration

The 3LO settings are **context / env only** — they have no `platform.yaml`
keys today. An empty client id means the provider is simply not created.

| cdk context | Env var | Default | Effect |
|---|---|---|---|
| `google_client_id` | `GOOGLE_CLIENT_ID` | `""` | Enables the Google 3LO provider |
| `google_client_secret_name` | `GOOGLE_CLIENT_SECRET_NAME` | `""` | Secrets Manager secret **name** for the Google client secret |
| `github_client_id` | `GITHUB_CLIENT_ID` | `""` | Enables the GitHub 3LO provider |
| `github_client_secret_name` | `GITHUB_CLIENT_SECRET_NAME` | `""` | Secret name for GitHub |
| `notion_client_id` | `NOTION_CLIENT_ID` | `""` | Enables the Notion 3LO provider |
| `notion_client_secret_name` | `NOTION_CLIENT_SECRET_NAME` | `""` | Secret name for Notion |

Setting a client id without its `*_client_secret_name` fails at synth with an
actionable message. A plaintext `*_client_secret` context key or env var is
rejected by `app.py` before synth even starts — `scripts/deploy.sh` stores the
value in Secrets Manager and passes the name automatically when the secret is
in the environment.

The gateway M2M provider's inputs (`gateway_m2m_client_id`,
`gateway_m2m_client_secret`, `cognito_discovery_url`) are not user
configuration: `app.py` wires them from the local auth stack, or from the
`deployment.federation` block in a federated workload account.

## Interfaces

SSM parameters under `/{project}/{environment}/identity/` (part of
[the platform interface](../PLATFORM_INTERFACE.md)):

| Parameter | Value |
|---|---|
| `gateway-credential-provider-name` | Provider name agents pass as `GATEWAY_CREDENTIAL_PROVIDER_NAME` |
| `{vendor}-provider-arn` | 3LO provider ARN — only for configured vendors |

Stack outputs (no exports): `GatewayCredentialProviderName`, plus
`GoogleProviderArn` / `GithubProviderArn` / `NotionProviderArn` when
configured. `app.py` injects `GATEWAY_CREDENTIAL_PROVIDER_NAME` into the
orchestrator and research-agent runtimes, whose
`get_gateway_access_token()` uses it with
`@requires_access_token(..., auth_flow="M2M")`
([outbound auth](../IDENTITY.md)).

## Security notes

- **`unsafe_unwrap()` here is deliberate and safe.** The L1 `client_secret`
  fields take plain strings, so the code unwraps `SecretValue`s — but what
  renders into the template is a CloudFormation *token* (an `Fn::GetAtt` on
  the auth stack's `DescribeUserPoolClient` custom resource for the M2M
  provider, a `{{resolve:secretsmanager:...}}` dynamic reference for the 3LO
  providers). CloudFormation resolves them at deploy time; no secret value
  appears in `cdk.out` or the synthesized template.
- OAuth **scopes are not provider configuration**: agents request them per
  token via `@requires_access_token(scopes=[...])`. For the M2M exchange,
  Cognito grants the scopes assigned to the M2M client (`agentcore/invoke`)
  when none are requested.
- The token vault stores each provider's secret in an AgentCore-owned Secrets
  Manager secret, and `GetResourceOauth2Token` reads it with the **caller's**
  identity — so the runtime role (`stacks/runtime_stack.py`) grants
  `secretsmanager:GetSecretValue` on
  `bedrock-agentcore-identity!default/oauth2/*`. Without it, gateway tool
  loading fails and takes the whole agent invocation down with it.
- The unverified-userId token path (`GetWorkloadAccessTokenForUserId`) is
  denied by an org-level SCP in `terraform/org-guardrails`, not by this stack
  — see [Security controls](../SECURITY_CONTROLS.md).

## Verification

No dedicated `scripts/verify.py` check. The gateway M2M provider is exercised
end to end by the orchestrator invoke (`scripts/invoke.py`): asking the agent
to list its tools forces the gateway MCP client through the token vault, which
is exactly the path [TESTING.md Part C](../TESTING.md) uses after the pattern
matrix. `tests/test_3lo_providers.py` guards the provider config at synth time
(see the first gotcha).

## Gotchas

- **Never pass raw dicts to the `aws_bedrockagentcore` L1s.** The 3LO blocks
  once passed a dict whose top-level key spelled OAuth with a capital A; the
  L1 mapping silently dropped the entire config and the template carried an
  empty provider config — deploys stayed green while the providers were broken
  from birth. The stack now uses the typed `*Property` classes, which raise at
  synth on a wrong key, and `tests/test_3lo_providers.py` pins it.
- `@requires_access_token` is evaluated at **import time** and
  `provider_name` defaults to `""` — a runtime missing
  `GATEWAY_CREDENTIAL_PROVIDER_NAME` binds an empty provider name at module
  load. The A2A `code-agent` intentionally does not receive it
  ([details](../IDENTITY.md)).
- In a federated workload account, the M2M client secret must exist in **that
  account's** Secrets Manager under `federation.m2m_client_secret_name` before
  deploying — the platform team hands over the name, never the value.
