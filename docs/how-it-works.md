<!-- markdownlint-disable MD041 -->

# How it works

The Agentic AI Platform EBA follows a **Design → Build → Verify** workflow:
you design the platform in one file, build it — and your first use case on it —
with one command each, and verify that the result keeps every promise the
design made. Everything else — governance, growth to multiple accounts, more
use cases — is an edit to the same file and another pass through the same
three commands.

![Architecture](architecture.png)

```bash
./scripts/deploy.sh design --profile greenfield   # 1. Design  — nothing deployed
./scripts/deploy.sh usecase new release-notes      #    (add a use case to the design)
./scripts/deploy.sh build                          # 2. Build   — platform + use cases
./scripts/deploy.sh verify                         # 3. Verify  — tests every claim
```

## 1. Design — `platform.yaml`

`deploy.sh design --profile <name>` materializes a preset into `platform.yaml`,
validates it, and prints what that design would produce:

```text
Design: agentcore-workshop-dev in us-east-1
  Topology: centralized
  Sign-in: entra_id
  Agent pattern: orchestrator
Stacks (7):
  agentcore-workshop-dev-auth
  agentcore-workshop-dev-identity
  agentcore-workshop-dev-memory
  agentcore-workshop-dev-gateway
  agentcore-workshop-dev-runtime-orchestrator
  agentcore-workshop-dev-observability
  agentcore-workshop-dev-uc-release-notes
Controls on: none (every control is opt-in)
Use cases:
  release-notes  (uc-release-notes)
Nothing has been deployed. Next: deploy.sh build, then deploy.sh verify.
```

Nothing is created; the only AWS call is the one that reads your account id.
Edit the file — switch the IdP, pick your agent framework, turn on a control —
and run `design` again until the plan is the platform you want:

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

use_cases:
  release-notes: {}        # your application, built on the platform
```

Validation is strict and errors are actionable: a typo'd key is an error, not
a silent no-op, every failure in the file is reported at once, and a preset
placeholder you forgot to replace (an IdP tenant id, an organization id) stops
`build` before it touches your account. Every key is documented in the
[platform.yaml reference](PLATFORM_YAML.md). Presets under `presets/` are
complete, tested starting points — `greenfield`, `migration`, `multi-agent`,
`platform-team`, `security-focused`, plus `federated` and `distributed`
for multi-account topologies.

From this file the platform derives its **deployment contract**: the exact
list of CloudFormation stacks your design produces. Build plans, verification,
the dashboard, and destroy all consume that contract — and CI fails if the
contract and the CDK app ever drift apart.

## 2. Build — platform and use case, one command

```bash
./scripts/deploy.sh build
```

Build runs ~20 preflight checks (credentials, region coherence, container
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
| `uc-<name>` | One stack per use case listed in the design |

Secrets never appear in code, context, or templates — they live in Secrets
Manager and only their *names* travel through configuration.

Each stack has a technical reference page — resources, configuration keys,
interfaces, IAM shape, verification hooks, and known gotchas — in the
[module reference](modules/README.md).

### Your use case is part of the build

```bash
./scripts/deploy.sh usecase new release-notes
```

scaffolds `use-cases/release-notes/` — a manifest, a CDK stack, a verify
script, and a walkthrough — and enables it in `platform.yaml`. The use case
consumes the platform only through its
[published interface](PLATFORM_INTERFACE.md) (SSM parameters for the gateway
URL, the user pool, the memory id, ...), so it deploys with the next `build`
and is torn down with `destroy`, with no changes to the platform itself.
`deploy.sh usecase list` shows what is discovered and what the design enables.

## 3. Verify — verification is a feature

```bash
./scripts/deploy.sh verify
```

Verification is derived from your design: it checks exactly what your platform
promises — gateway tools respond to a real MCP `tools/call`, memory stores and
recalls events, traces are flowing, alarms exist, IAM actually denies
ungoverned inference — then runs each use case's own `verify.py`, and exits
non-zero on any failure. The same checks gate this repository's releases.

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
identity, and model choice move out of the agent into managed services. The
`migration` profile goes one step further and lands your *existing* container
on the platform behind an adapter — `deploy.sh migrate plan` shows the path.

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
