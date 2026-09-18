<!-- markdownlint-disable MD041 -->

# Auth module

The auth module (`stacks/auth_stack.py`) declares the platform's token
issuer: the one OIDC issuer every other component validates JWTs against. The
gateway and the client-facing runtimes each run a `CUSTOM_JWT` authorizer
pinned to it, and the agents verify the same tokens a second time in code
([Verify caller identity](../IDENTITY.md)). It has two modes, chosen in
Design with `identity.mode`:

- **`brokered`** (default) — an Amazon Cognito user pool is the issuer.
  Optionally an enterprise IdP (Entra ID, Okta, Ping) federates in via OIDC:
  users sign in at the corporate IdP, but the tokens agents see are minted by
  Cognito, so nothing downstream changes
  ([Enterprise IdP federation](../ENTERPRISE_IDP.md)).
- **`direct`** — your Entra ID tenant is the issuer. No user pool is created;
  the stack only publishes the issuer facts to the same `/auth/*` interface,
  and the authorizers pin the token **audience** (your app's client id)
  instead of Cognito client ids
  ([Direct mode](../ENTERPRISE_IDP.md#direct-mode-entra-id-issues-the-tokens)).

## When it deploys

| Configuration | Deployed? |
|---|---|
| Centralized / distributed (the default) | Always |
| Federated, platform account | Yes — auth is a shared service |
| Federated, workload account | No — the account consumes the platform account's issuer via the `deployment.federation` block |

Source: `expected_stacks()` in `infra_utils/platform_config.py` adds
`{prefix}-auth` when the federated role is not `workload`; `app.py` skips
`AuthStack` under the same condition. `identity.mode` does not change *whether*
the stack deploys, only what it contains — the contract, the dashboard and
`destroy` stay the same shape in both modes.

## What it creates

### `mode: brokered`

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
| 7 × `AWS::SSM::Parameter` | `/{project}/{environment}/auth/*` | Cross-stack / cross-account discovery (see Interfaces) |

### `mode: direct`

| Resource | Name pattern | Purpose |
|---|---|---|
| 6 × `AWS::SSM::Parameter` | `/{project}/{environment}/auth/*` | The issuer facts: `mode=direct`, Entra's v2 issuer, your app's client id as both `app-client-id` and `m2m-client-id`, the `.default` M2M scope, and the **name** of the Secrets Manager secret holding the client secret |

Nothing else. The Entra app registration serves humans (authorization code)
and machines (`client_credentials`) alike; the identity module's gateway
credential provider points at Entra's discovery document and reads the same
secret.

## Configuration

Precedence everywhere: cdk context > env var > `platform.yaml` > default.

| cdk context | Env var | `platform.yaml` | Default | Effect |
|---|---|---|---|---|
| `idp_type` | `IDP_TYPE` | `identity.idp` | `cognito` | `cognito`, `entra_id`, `okta`, or `ping` |
| `idp_mode` | `IDP_MODE` | `identity.mode` | `brokered` | `brokered` (Cognito issues) or `direct` (the IdP issues — `entra_id` only; okta/ping stay brokered) |
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

| Parameter | brokered | direct |
|---|---|---|
| `mode` | `brokered` | `direct` — read this first; absent on platforms deployed before it existed (= brokered) |
| `issuer-url` | `https://cognito-idp.{region}.amazonaws.com/{pool-id}` | `https://login.microsoftonline.com/{tenant}/v2.0` |
| `user-pool-id` | The user pool id | — |
| `app-client-id` | Authorization-code client (humans) | Your Entra app's client id (humans and machines) |
| `web-client-id` | SRP client (browsers) | — |
| `m2m-client-id` | `client_credentials` client (machines) | The same Entra client id |
| `m2m-scope` | `agentcore/invoke` | `{client_id}/.default` |
| `m2m-client-secret-name` | — (the secret lives in Cognito) | Secrets Manager **name** of the client secret |

Stack outputs (no exports): `IssuerUrl`, `DiscoveryUrl`, `AppClientId`,
`M2MClientId`, `IdPType`, `IdPMode` in both modes; brokered adds
`UserPoolId`, `UserPoolArn`, `WebClientId`, `DomainUrl`. Consumers wired by
`app.py`: the gateway and runtime authorizers (issuer + allowed clients, or
issuer + allowed audience in direct mode — `infra_utils/jwt_authorizer.py`
renders both), the identity module (M2M client id and secret for the gateway
credential provider), and the `COGNITO_ISSUER_URL` / `COGNITO_ALLOWED_CLIENTS`
env vars on the orchestrator runtime (in direct mode they carry Entra's issuer
and the audience; the names are kept for compatibility). Direct mode adds
`GATEWAY_TOKEN_SCOPES` so agents request the `.default` scope Entra requires.
In a federation, `deploy.sh export` hands `issuer_url` and `m2m_client_id` to
workload accounts in either mode ([Multi-account federation](../MULTI_ACCOUNT.md)).

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
  **client id (brokered) or audience (direct) — not scopes**. A user token
  without `agentcore/invoke` is accepted; enforce scopes in the agent if you
  need them ([details](../IDENTITY.md)).
- **Direct mode widens who can mint a platform token**: anyone who can obtain
  a token from your tenant for that app's audience. Entra's own controls
  (app role assignment required, conditional access) are the gate — the
  platform does not add one.
- Anyone with `cognito-idp:DescribeIdentityProvider` can read the federated
  IdP's client secret from `ProviderDetails` — AWS behaviour, not something
  this stack can hide. Treat that permission as sensitive.

## Verification

`scripts/check_identity.py` runs **first** in `deploy.sh verify` for any
footprint with an auth stack: it compares the deployed `auth/mode` with the
design, and in direct mode mints a token exactly as the agents do and checks
its `iss` against the discovery document and its `aud` against the allowed
audience — naming the fix for the two Entra prerequisites below. Every later
check then exercises the issuer for real: `invoke.py` and `test_gateway.py`
both obtain an M2M token (`scripts/utils.py get_m2m_token`, which reads
`auth/mode` and switches endpoints), so a broken auth stack fails the whole
footprint. Preflight, `scripts/check-deploy-config.sh`
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
- **Direct mode, Entra side (both fail silently at build, loudly at first
  invoke):** the app needs a *service principal* in the tenant (`az ad sp
  create --id <client-id>` — Cognito federation creates one lazily at first
  sign-in, `client_credentials` never does; otherwise `AADSTS7000229`), and it
  must request **v2 access tokens** (`api.requestedAccessTokenVersion: 2`,
  set via a Graph PATCH — `az ad app update --set api.…` cannot); otherwise
  tokens carry the v1 issuer `https://sts.windows.net/<tenant>/` and every
  authorizer rejects them. `check_identity.py` reports both by name.
- **Switching a live platform from brokered to direct** cannot go auth-first:
  the gateway, identity and runtime stacks import Cognito values as
  CloudFormation exports, and CloudFormation refuses to update auth while an
  export is imported. `deploy.sh build` detects the switch and deploys the
  consumers first (check v in `check-deploy-config.sh` pins the order). The
  reverse switch works in the default order. Either way the user pool is
  deleted and recreated — its users do not survive the round trip.
