<!-- markdownlint-disable MD041 -->

# Auth module

The auth module (`stacks/auth_stack.py`) is the platform's token issuer: an
Amazon Cognito user pool that every other component validates JWTs against.
The gateway and the client-facing runtimes each run a `CUSTOM_JWT` authorizer
pinned to this pool's issuer, and the agents verify the same tokens a second
time in code ([Verify caller identity](../IDENTITY.md)). Optionally, an
enterprise IdP (Entra ID, Okta, Ping) federates in via OIDC — users sign in at
the corporate IdP, but the tokens agents see are still minted by Cognito, so
nothing downstream changes ([Enterprise IdP federation](../ENTERPRISE_IDP.md)).

## When it deploys

| Configuration | Deployed? |
|---|---|
| Centralized / distributed (the default) | Always |
| Federated, platform account | Yes — auth is a shared service |
| Federated, workload account | No — the account consumes the platform account's issuer via the `deployment.federation` block |

Source: `expected_stacks()` in `infra_utils/platform_config.py` adds
`{prefix}-auth` when the federated role is not `workload`; `app.py` skips
`AuthStack` under the same condition.

## What it creates

| Resource | Name pattern | Purpose |
|---|---|---|
| `AWS::Cognito::UserPool` | `{prefix}-user-pool` | Email sign-in, self-signup enabled, email auto-verify, email-only recovery |
| Cognito user pool domain | `{prefix}-{account}` | Hosted UI / OAuth endpoints (`…​.auth.{region}.amazoncognito.com`) |
| `AWS::Cognito::UserPoolResourceServer` | identifier `agentcore` | Defines the custom scope `agentcore/invoke` |
| App client | `{prefix}-app-client` | Authorization-code grant, has a secret — humans via the hosted UI |
| Web client | `{prefix}-web-client` | SRP + implicit grant, no secret — browser SPAs |
| M2M client | `{prefix}-m2m-client` | `client_credentials` grant, scope `agentcore/invoke` — machines |
| `UserPoolIdentityProviderOidc` (optional) | `EntraID` / `Okta` / `PingIdentity` | Federated OIDC provider, created only when `idp_type` is not `cognito` |
| CDK custom resource + singleton Lambda | (CDK-generated) | Backs the `m2m_client_secret` property: calls `DescribeUserPoolClient` so the secret reaches consumers as a CloudFormation token, never literal text |
| 5 × `AWS::SSM::Parameter` | `/{project}/{environment}/auth/*` | Cross-stack / cross-account discovery (see Interfaces) |

## Configuration

Precedence everywhere: cdk context > env var > `platform.yaml` > default.

| cdk context | Env var | `platform.yaml` | Default | Effect |
|---|---|---|---|---|
| `idp_type` | `IDP_TYPE` | `identity.idp` | `cognito` | `cognito`, `entra_id`, `okta`, or `ping` |
| `idp_tenant_id` | `IDP_TENANT_ID` | `identity.tenant_id` | `""` | Entra ID only; builds the issuer URL |
| `idp_client_id` | `IDP_CLIENT_ID` | `identity.client_id` | `""` | The IdP application's client id |
| `idp_issuer_url` | `IDP_ISSUER_URL` | `identity.issuer_url` | `""` | Okta / Ping only (cannot be derived) |
| `idp_client_secret_name` | `IDP_CLIENT_SECRET_NAME` | `identity.client_secret_name` | `""` | Secrets Manager secret **name** holding the IdP client secret — never the value |

The stack accepts `callback_urls` / `logout_urls` parameters, but `app.py`
does not pass them today, so the defaults always apply:
`http://localhost:3000/api/auth/callback/cognito` and
`http://localhost:3000`. A `redirect_mismatch` from Cognito means your client
used a different redirect URI.

## Interfaces

SSM parameters under `/{project}/{environment}/auth/` (part of
[the platform interface](../PLATFORM_INTERFACE.md)):

| Parameter | Value |
|---|---|
| `issuer-url` | `https://cognito-idp.{region}.amazonaws.com/{pool-id}` |
| `user-pool-id` | The user pool id |
| `app-client-id` | Authorization-code client (humans) |
| `web-client-id` | SRP client (browsers) |
| `m2m-client-id` | `client_credentials` client (machines) |

Stack outputs (no exports): `UserPoolId`, `UserPoolArn`, `IssuerUrl`,
`DiscoveryUrl`, `AppClientId`, `WebClientId`, `M2MClientId`, `DomainUrl`,
`IdPType`. Consumers wired by `app.py`: the gateway and runtime authorizers
(issuer + allowed clients), the identity module (M2M client id and secret for
the gateway credential provider), and the `COGNITO_ISSUER_URL` /
`COGNITO_ALLOWED_CLIENTS` env vars on the orchestrator runtime. In a
federation, `deploy.sh export` hands `issuer_url` and `m2m_client_id` to
workload accounts ([Multi-account federation](../MULTI_ACCOUNT.md)).

## Security notes

- **The IdP client secret is name-only configuration.** The stack refuses to
  synth a federated IdP without `idp_client_secret_name`, and `app.py` rejects
  any plaintext secret key outright. The value reaches CloudFormation as a
  `{{resolve:secretsmanager:...}}` dynamic reference — it never appears in
  `cdk.out`, the template, or process arguments.
- **The user pool has `removal_policy=DESTROY`**: destroying the stack deletes
  the pool and every user in it. Fine for a workshop; reconsider before
  holding real users.
- **Self-signup is enabled** — anyone who can reach the hosted UI can register
  an email address. The password policy is 8+ chars with upper/lower/digits,
  no symbols required.
- The runtime and gateway authorizers validate signature, issuer, and
  **client id — not scopes**. A user token without `agentcore/invoke` is
  accepted; enforce scopes in the agent if you need them
  ([details](../IDENTITY.md)).
- Anyone with `cognito-idp:DescribeIdentityProvider` can read the federated
  IdP's client secret from `ProviderDetails` — AWS behaviour, not something
  this stack can hide. Treat that permission as sensitive.

## Verification

There is no dedicated auth check in `scripts/verify.py` — instead every check
that mints a token exercises it: `invoke.py` and `test_gateway.py` both obtain
an M2M token from this pool (`scripts/utils.py get_m2m_token`), so a broken
auth stack fails the whole footprint. Preflight, `scripts/check-deploy-config.sh`
proves the IdP secret is trimmed before storage (check k) and that a
bring-your-own secret name is reused, not duplicated (check l). For federated
IdPs, [Enterprise IdP federation](../ENTERPRISE_IDP.md) has four browserless
checks that isolate failures before a human signs in.

## Gotchas

- **Trailing whitespace in the IdP client secret** is the classic failure:
  stored verbatim, forwarded verbatim by Cognito, rejected by the IdP with
  `invalid_client` and no mention of whitespace. `deploy.sh` trims the value
  before storing it; if you create the secret yourself, pipe through
  `tr -d '\n\r'` ([step-by-step](../ENTERPRISE_IDP.md)).
- Entra ID apps need `--enable-id-token-issuance`; without it the login fails
  *after* the user has authenticated.
- `invalid_client_secret` from Cognito's own `/oauth2/token` usually means you
  sent another client's secret — the app client and the M2M client each have
  their own.
- Switching `idp_type` back to `cognito` removes the provider but **orphans
  its users**: they remain as `EXTERNAL_PROVIDER` accounts nobody can sign in
  as, until deleted with `admin-delete-user`.
