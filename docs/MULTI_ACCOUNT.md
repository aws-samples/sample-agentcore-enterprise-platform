# Multi-Account Strategies

`platform.yaml` supports three deployment strategies via `deployment.strategy`.
This document explains what each one means, when to choose it, and how the
federated trust model works — including what was verified live across two
accounts.

## Choosing a strategy

| | `centralized` | `distributed` | `federated` |
|---|---|---|---|
| **Accounts** | One | One per team, full copy each | One platform + N workload |
| **Gateway & tools** | Local | Per-account | **Shared** (platform account) |
| **Cognito / IdP** | Local | Per-account | **Shared** (platform account) |
| **Agent runtimes** | Local | Per-account | Workload accounts only |
| **Memory** | Local | Per-account | **Per-workload** (see below) |
| **Observability** | Local | Per-account | Per-account (each monitors what it runs) |
| **Org guardrails (SCPs)** | n/a | `control-library/terraform/org-guardrails`, org-wide | `control-library/terraform/org-guardrails`, org-wide |
| **Choose when** | Workshops, pilots, single team | Strong team autonomy, simple trust | Central tool governance, many agent teams |

**Centralized** is the default and what every workshop module assumes.

**Distributed** is centralized, repeated: each team deploys the same
`platform.yaml` (different `project`/account) into its own account. There is
no cross-account trust to design; governance comes from the org-level SCPs,
which attach once at the OU level and constrain every copy.

**Federated** centralizes the expensive-to-govern pieces — the gateway (one
approved tool catalog, one place for Cedar policies and egress filtering) and
the identity provider — while each agent team runs its own runtimes in its own
account.

## Federated: the trust model

**Cross-account trust is pure OAuth. There is no cross-account IAM anywhere on
the data plane.** Verified live (platform `…1817`, workload `…4125`, us-east-1):

```
Workload account                         Platform account
┌──────────────────────────┐             ┌──────────────────────────┐
│ Agent runtime            │             │ Cognito (M2M app client) │
│  │ 1. GetResourceOauth2  │             │        ▲                 │
│  ▼    Token (own vault)  │             │        │ 2. client_creds │
│ Credential provider ─────┼─────────────┼────────┘    exchange     │
│  (holds platform M2M     │             │                          │
│   client id + secret)    │             │ AgentCore Gateway        │
│  │ 3. Bearer JWT         │             │  │ validates JWT against │
│  └───────────────────────┼─────────────┼─►│ its own Cognito       │
│                          │             │  ▼                       │
│                          │             │ Tools (web search, …)    │
└──────────────────────────┘             └──────────────────────────┘
```

1. The workload account's **own** AgentCore Identity credential provider holds
   the platform Cognito M2M client credentials (token vaults are
   account-local; the secret sits in the workload account's Secrets Manager).
2. The token vault exchanges them at the platform Cognito token endpoint
   (`client_credentials` grant) — standard OAuth, works cross-account because
   it is just HTTPS.
3. The gateway validates the resulting JWT against its own issuer. It cannot
   tell (and does not care) which account the caller runs in.

**Memory stays per-workload by design.** Conversation history is the tenant
boundary (`actor_id`), and account isolation is the strongest wall available —
sharing one memory store across teams would undo it. Each workload account
deploys its own memory stack; nothing memory-related crosses accounts.

## Federated: how to deploy

The **same `platform.yaml` deploys both sides** — the account you deploy into
determines the role:

```yaml
deployment:
  strategy: federated
  platform_account: "111122223333"
  workload_accounts: ["444455556666"]
  federation:                  # filled by the platform team after step 1
    gateway_url: https://...gateway...amazonaws.com/mcp
    issuer_url: https://cognito-idp.<region>.amazonaws.com/<pool>
    m2m_client_id: <client id>
    m2m_client_secret_name: agentcore/platform-m2m   # name, never the value
```

**1. Platform team** deploys into the platform account
(`auth`, `identity`, `gateway`, `observability` — no runtimes):

```bash
AWS_PROFILE=platform ./scripts/deploy.sh deploy
```

Then collects the federation values from stack outputs (none is secret):

```bash
aws cloudformation describe-stacks --stack-name <prefix>-auth \
  --query "Stacks[0].Outputs[?OutputKey=='IssuerUrl'||OutputKey=='M2MClientId'].OutputValue"
aws ssm get-parameter --name /<project>/<env>/gateway/url --query Parameter.Value
```

**2. Platform team hands the workload team** the four federation values plus
the M2M client secret through a secure channel. The secret is read from the
Secrets Manager name published at `/<project>/<env>/auth/m2m-client-secret-name`;
it is never a CloudFormation output. The workload team stores it:

```bash
aws secretsmanager create-secret --name agentcore/platform-m2m \
  --secret-string file:///dev/stdin   # paste, never argv
```

**3. Workload team** fills the `federation:` block and deploys into its
account (`identity` with the platform credentials, `memory`, runtimes,
`observability`):

```bash
AWS_PROFILE=workload ./scripts/deploy.sh deploy
```

Deploying a federated file from an account in neither list fails at synth with
a message naming both — the config-file version of "wrong account".

## What each side can and cannot do

- The workload agent gets exactly the tools the platform gateway exposes —
  tool governance (Cedar, egress filter, target approval) is enforced in one
  place, and workload teams cannot widen it.
- Federated M2M client rotation is coordinated because each workload has its
  own credential provider and local secret copy. The first platform build
  creates the replacement, updates platform authorizers to accept both client
  IDs, and then stops before deleting the old client. The safe
  `M2MClientIdV2Export` and `M2MClientSecretNameV2Export` auth-stack outputs
  identify the replacement; retrieve the value only from that named secret and
  transfer it through the team's approved secret channel. After the platform
  preparation stops, `./scripts/deploy.sh export` produces a mode-`0600`,
  Git-ignored snapshot containing those two references but never the secret
  value.

  For each workload, preserve the previous `m2m_client_id`, update the
  federation client ID and local secret, then deploy while allowing already
  issued old-client JWTs to drain:

  ```bash
  FEDERATED_RETIRED_M2M_CLIENT_ID=<previous-client-id> \
    AWS_PROFILE=workload ./scripts/deploy.sh deploy
  ```

  Only after every workload succeeds, rerun the platform build with
  `FEDERATED_M2M_CONSUMERS_UPDATED=1`; this acknowledgement permits old-client
  deletion and starts the 65-minute drain. After that drain, deploy each
  workload once without `FEDERATED_RETIRED_M2M_CLIENT_ID` to remove the retired
  ID from its runtime authorizers.
- The platform account sees gateway-side telemetry for all teams;
  workload accounts see their own runtime/memory telemetry. Neither sees the
  other's logs.
- The runtime's inbound JWT authorizer also points at the platform issuer, so
  callers of workload agents authenticate against the same identity plane.

## Verified

- Cross-account gateway access (workload credential provider → platform
  Cognito → platform gateway `tools/list` + `tools/call` with a real web
  search): verified live, 2026-08-16.
- Role-based stack selection from one file (platform: auth/identity/gateway/
  observability; workload: identity/memory/runtime/observability): synth
  assertions in `tests/test_platform_config.py`, plus a live two-account
  deployment — see the PR that introduced this document for the transcript.
