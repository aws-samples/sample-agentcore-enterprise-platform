<!-- markdownlint-disable MD041 -->

# How it works

The accelerator follows a **Declare → Deploy → Prove** workflow: you declare
the platform in one file, deploy it with one command, and prove it works with
another. Everything else — governance, growth to multiple accounts, your own
use cases — is an edit to the same file.

![Architecture](architecture.png)

## 1. Declare — `platform.yaml`

One typed, validated configuration file is the source of truth for the entire
deployment:

```yaml
project: agentcore-workshop
environment: dev
region: us-east-1

identity:
  idp: entra_id            # or cognito, okta, ping

agents:
  pattern: langgraph-agent # bring your framework — 7 patterns
  model_id: us.anthropic.claude-sonnet-4-6
  allowed_models:          # IAM-enforced model allow-list
    - us.anthropic.claude-sonnet-4-6

security:
  require_guardrails: true # inference without a guardrail = AccessDenied

observability:
  alarms: true             # CloudWatch alarms + platform dashboard
```

Validation is strict and errors are actionable: a typo'd key is an error, not
a silent no-op, and every failure in the file is reported at once. Presets
under `presets/` are complete, tested starting points — `greenfield`,
`migration`, `multi-agent`, `platform-team`, `security-focused`.

From this file the platform derives its **deployment contract**: the exact
list of CloudFormation stacks your configuration produces. Deploy plans,
verification, the dashboard, and destroy all consume that contract — and CI
fails if the contract and the CDK app ever drift apart.

## 2. Deploy — one command

```bash
./scripts/deploy.sh deploy --profile greenfield
```

The wizard runs ~20 preflight checks (credentials, region coherence, container
tooling, secret hygiene), prints the stack plan from the contract, and asks
before creating anything. Under an hour later:

| Stack | Role |
|---|---|
| `auth` | Cognito user pool, app clients, optional IdP federation |
| `identity` | AgentCore Identity — workload identities, OAuth credential providers |
| `memory` | AgentCore Memory store (short-term; long-term opt-in) |
| `gateway` | AgentCore Gateway (MCP) with Lambda tools, web search, optional Cedar + egress guardrail |
| `runtime-*` | Your agent containers on AgentCore Runtime, built by CodeBuild, rebuilt only when source changes |
| `observability` | Vended logs, Transaction Search, optional alarms + dashboard |

Secrets never appear in code, context, or templates — they live in Secrets
Manager and only their *names* travel through configuration.

## 3. Prove — verification is a feature

```bash
python scripts/verify.py
```

Verification is derived from your configuration: it checks exactly what your
platform promises — gateway tools respond to a real MCP `tools/call`, memory
stores and recalls events, traces are flowing, alarms exist, IAM actually
denies ungoverned inference — and exits non-zero on any failure. The same
checks gate this repository's releases.

## Governance is enforcement, not convention

With the security controls on:

- **Model allow-list** — the runtime role's Bedrock permissions are scoped to
  exactly the models in `agents.allowed_models`. An unapproved model is an
  `AccessDenied`, even against a container's baked-in default.
- **Guardrailed-only inference** — every runtime gets a baseline Bedrock
  Guardrail, and an IAM deny makes any inference call *without* a guardrail
  fail. Not a code-review convention: IAM.
- **Cedar authorization** on gateway tools, **CloudTrail-backed alerting** on
  sensitive configuration changes, **resource policies**, and optional
  **VPC networking** — each an independent flag, each verified by `verify`.

See [Security controls](SECURITY_CONTROLS.md).

## Bring your framework

The `agents.pattern` setting selects which agent ships onto identical
infrastructure — Strands, LangGraph, Claude Agent SDK (single and multi-agent),
AG-UI variants, or the delegating orchestrator. Migration is not a rewrite:
your LangGraph code stays LangGraph; what changes is that tools, state,
identity, and model choice move out of the agent into managed services.

## Grow when ready

The same `platform.yaml` deploys a **federated multi-account** topology:
shared services in a platform account, agent runtimes in workload accounts,
trust based purely on OAuth token exchange — no cross-account IAM roles on the
data plane. The account you deploy into decides which half comes up. See
[Multi-account federation](MULTI_ACCOUNT.md).

Your applications join as **use cases**: a self-contained folder with a
manifest, a stack, a verify script, and a walkthrough, consuming the platform
only through its published interface. See
[the platform interface](PLATFORM_INTERFACE.md).
