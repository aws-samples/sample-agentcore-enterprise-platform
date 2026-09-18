# platform.yaml reference

`platform.yaml` is the one file that describes a deployment: project identity,
account strategy, IdP, agent pattern, gateway tools, security controls,
observability, and (for a migration) the agent you are bringing. Everything
else — the stack list, verification, the dashboard, destroy — is derived from
it, so a typo'd key is an error and every failure in the file is reported at
once.

You rarely write it from scratch: `deploy.sh design --profile <name>` writes it
from the matching `presets/<name>.yaml` (the presets are embedded verbatim at
the end of this page). Edit the result, then validate without touching AWS:

```bash
python -m infra_utils.platform_config platform.yaml
```

Precedence when a value is set in more than one place:
`cdk context > env var > platform.yaml > defaults`. The **Env override**
column is the name `to_env()` exports for each key — the same name `deploy.sh`
and `app.py` read, so an exported variable wins over the file for that run
only. Keys marked ✦ change **which stacks** are deployed
(`expected_stacks()`), not just how a stack is configured; a plan is printed
before anything is created.

✦ changes the deployment footprint (the set of stacks), see `expected_stacks()`.

## Top level

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `project` | str (matches `^[a-z][a-z0-9-]{2,32}$`) | `'agentcore-workshop'` | `PROJECT_NAME` | Name prefix for every stack, role and SSM parameter (`{project}-{environment}-…`). Changing it is a new deployment, not a rename. |
| `environment` | str (matches `^[a-z][a-z0-9]{1,15}$`) | `'dev'` | `ENVIRONMENT` | Second half of the stack prefix; lets one account hold `dev` and `prod` side by side. |
| `region` | str (matches `^[a-z]{2}(-[a-z]+)+-\d$`) | `'us-east-1'` | `AWS_REGION` | Where everything deploys. Also decides whether `gateway.web_search: auto` turns on (launch regions only). |
| `use_cases` ✦ | map of str → object | `{}` | — | Opt-in use cases: name → that use case's own config block, passed to its `build()` untouched (`{}` enables with defaults). Names must exist under `use-cases/`, so a typo is an error, not a silent no-op. Their stacks ride at the end of the footprint. |

## `deployment:`

Multi-account strategy. See docs/MULTI_ACCOUNT.md.

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `deployment.strategy` ✦ | one of: `centralized`, `distributed`, `federated` | `'centralized'` | `DEPLOYMENT_STRATEGY` | `centralized` puts everything in one account. `distributed` means each team deploys its own copy of this file. `federated` splits shared services (auth, gateway) into `platform_account` from agent runtimes in `workload_accounts`; the account you deploy into decides the role, the same file works in both. |
| `deployment.platform_account` | str | `""` | `PLATFORM_ACCOUNT` | 12-digit account that hosts the shared services in a `federated` deployment. Required by that strategy, ignored by the others. |
| `deployment.workload_accounts` | list of str | `[]` | — | 12-digit accounts that run agent runtimes in a `federated` deployment. Deploying a federated file from an account in neither list is a hard error. |
| `deployment.federation.gateway_url` | str | `""` | — | Platform-account gateway MCP endpoint a workload account calls (an output of the platform gateway stack). |
| `deployment.federation.issuer_url` | str | `""` | — | Platform Cognito issuer; the OIDC discovery URL is derived from it. |
| `deployment.federation.m2m_client_id` | str | `""` | — | Platform Cognito M2M client id the workload's token vault exchanges for a JWT. Not secret. |
| `deployment.federation.m2m_client_secret_name` | str | `""` | — | Secrets Manager NAME in the workload account holding the M2M client secret. The secret itself never goes in this file. |

## `identity:`

Who issues the tokens the platform trusts.

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `identity.idp` | one of: `cognito`, `entra_id`, `okta`, `ping` | `'cognito'` | `IDP_TYPE` | Who authenticates users. `cognito` is self-contained; the others federate Cognito to your enterprise IdP (docs/ENTERPRISE_IDP.md). |
| `identity.mode` | one of: `brokered`, `direct` | `'brokered'` | `IDP_MODE` | Who issues the tokens. `brokered`: Cognito issues them and the IdP only signs users in — works with no IdP at all. `direct`: the IdP issues them and no Cognito user pool is deployed; `entra_id` only, and the app registration needs a service principal and v2 access tokens (docs/ENTERPRISE_IDP.md, Direct mode). |
| `identity.tenant_id` | str | `""` | `IDP_TENANT_ID` | Entra ID tenant. Required when `idp: entra_id`. |
| `identity.client_id` | str | `""` | `IDP_CLIENT_ID` | App registration / OIDC client id at your IdP. |
| `identity.issuer_url` | str | `""` | `IDP_ISSUER_URL` | OIDC issuer of your IdP. Required for `okta` and `ping` (Entra derives it from the tenant). |
| `identity.client_secret_name` | str | `""` | `IDP_CLIENT_SECRET_NAME` | Secrets Manager NAME holding the IdP client secret. Required for any non-Cognito IdP; the value never goes in this file. |

## `agents:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `agents.pattern` | one of: `orchestrator`, `strands-agent`, `langgraph-agent`, `claude-sdk-agent`, `claude-sdk-multi-agent`, `agui-strands-agent`, `agui-langgraph-agent` | `'orchestrator'` | `AGENT_PATTERN` | Which agent container the runtime stack builds (docs/ARCHITECTURE.md). Ignored for the image once `migration:` is set, but still exported so verify and invoke behave. |
| `agents.model_id` | str | `""` | `MODEL_ID` | Bedrock model or cross-region inference profile injected as `MODEL_ID` into every agent. Empty = each pattern's baked-in default. |
| `agents.allowed_models` | list of str | `[]` | `ALLOWED_MODELS` | Model allow-list. When set, the runtime roles' Bedrock IAM is scoped to exactly these and `model_id` must be one of them (otherwise the containers' defaults would bypass the list). Empty = unrestricted, today's behaviour. |
| `agents.a2a` ✦ | bool | `False` | `ENABLE_A2A` | Deploy the `code-agent` and `research-agent` sub-agent runtimes next to the orchestrator (A2A protocol). |
| `agents.memory.long_term` | bool | `False` | `USE_LONG_TERM_MEMORY` | Add the semantic long-term strategy to the AgentCore Memory store (fact extraction across sessions). |
| `agents.memory.top_k` | int (1–100) | `10` | `LTM_TOP_K` | Long-term retrieval: how many records to pull per query. |
| `agents.memory.relevance_score` | float (0.0–1.0) | `0.3` | `LTM_RELEVANCE_SCORE` | Long-term retrieval: minimum relevance for a record to be returned. |

## `gateway:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `gateway.web_search` | one of: `auto`, `on`, `off` | `'auto'` | `ENABLE_WEB_SEARCH` | Built-in web-search connector on the gateway. `auto` turns it on only in regions where the connector exists. |
| `gateway.tools` | list of str | `['sample-tool']` | — | Lambda tool names the gateway exposes. Schema-validated today; the deployed list still comes from `tool_configs` in `app.py` (docs/modules/gateway.md). |

## `security:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `security.networking` ✦ | bool | `False` | `ENABLE_NETWORKING` | VPC mode: runtimes in private subnets behind a NAT gateway and VPC endpoints. Adds the `networking` stack; required by `migration.target.runtime: ec2` and by private dependencies. |
| `security.cloudtrail_alerting` ✦ | bool | `False` | `ENABLE_SECURITY` | The `security` stack: a CloudTrail trail plus alerting. Needed for `traceability` to fire at all. |
| `security.resource_policies` | bool | `False` | `ENABLE_RESOURCE_POLICIES` | In-account-only resource policy on the Memory store, with an org deny guard. Needs `org_id`. |
| `security.egress_filter` | bool | `False` | `ENABLE_EGRESS_FILTER` | Bedrock Guardrail plus an interceptor Lambda on the gateway; masks PII in tool traffic rather than blocking it (docs/SECURITY_CONTROLS.md). |
| `security.require_guardrails` | bool | `False` | `REQUIRE_GUARDRAILS` | IAM-denies Bedrock inference without a Guardrail on the runtime roles, creates a baseline guardrail per runtime and injects it into the agents. Incompatible with the `claude-sdk-*` patterns, which cannot attach one. |
| `security.cedar.enabled` | bool | `False` | `ENABLE_CEDAR` | Cedar policy engine on the gateway (tool-level authorization). |
| `security.cedar.mode` | one of: `LOG_ONLY`, `ENFORCE` | `'LOG_ONLY'` | `CEDAR_MODE` | `LOG_ONLY` records decisions without enforcing; flip to `ENFORCE` only after reading the logs on live traffic. |
| `security.traceability` | bool | `False` | `ENABLE_TRACEABILITY` | SNS + EventBridge alerts on sensitive API calls. Requires `cloudtrail_alerting`, the rule only matches CloudTrail management events. |
| `security.org_id` | str | `""` | `ORG_ID` | AWS Organizations id (`o-…`) the VPC endpoint and Memory policies are scoped to. Empty is fine (deploy.sh prompts); a placeholder is rejected. |

## `observability:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `observability.transaction_search` | bool | `True` | `ENABLE_TRANSACTION_SEARCH` | Configure CloudWatch Transaction Search so runtime traces are searchable. Account-scoped, not per stack. |
| `observability.alarms` | bool | `False` | `ENABLE_ALARMS` | CloudWatch alarms per deployed resource, an SNS ops topic and the platform dashboard. |
| `observability.alarm_email` | str | `""` | `ALARM_EMAIL` | Inbox subscribed to the alarm topic (SNS sends a confirmation first). Empty = topic without subscription; placeholders are rejected. |

## `migration:`

Migrate an existing agent onto the platform.

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `migration` | block, optional | absent | `MIGRATION_ENABLED` | Move an existing agent onto the platform. Absent = no migration, everything behaves as before. Stack names do not change: the migrated agent deploys as `…-runtime-orchestrator` on both targets. |
| `migration.source.platform` | one of: `openshift`, `kubernetes`, `ec2`, `ecs`, `lambda`, `on-prem`, `other` | **required** | `MIGRATION_SOURCE_PLATFORM` | Where the agent runs today. Shapes the docs and the plan, not the deploy. |
| `migration.source.image` | str | `""` | `MIGRATION_SOURCE_IMAGE` | Pre-built image reference. Exactly one of `image` or `build`. AgentCore Runtime is arm64-only and a pre-built image cannot be checked for that before deploy, so the plan warns. |
| `migration.source.build` | block, optional | absent | — | Build the image here from the customer's Dockerfile. Yields arm64 by construction, which is why the shipped preset uses it. Exactly one of `image` or `build`. |
| `migration.source.build.context` | str | **required** | `MIGRATION_BUILD_CONTEXT` | Docker build context, relative to the repo root. |
| `migration.source.build.dockerfile` | str | `'Dockerfile'` | `MIGRATION_BUILD_DOCKERFILE` | Dockerfile path, relative to `context`. |
| `migration.source.registry_secret_name` | str | `""` | `MIGRATION_REGISTRY_SECRET_NAME` | Secrets Manager NAME holding docker-login credentials for a private registry. |
| `migration.source.port` | int (1–65535), optional | absent | `MIGRATION_PORT` | What the container listens on. Required in `adapter` mode; must be absent in `native` mode. |
| `migration.source.invoke_path` | str | `""` | `MIGRATION_INVOKE_PATH` | Where the container takes a request; the adapter forwards `POST /invocations` there. Required in `adapter` mode; absent in `native`. |
| `migration.source.health_path` | str | `'/'` | `MIGRATION_HEALTH_PATH` | What the adapter polls to answer `GET /ping`. |
| `migration.source.trigger` | one of: `webhook`, `schedule`, `http`, `queue` | `'http'` | `MIGRATION_TRIGGER` | How work arrives today. Documented for the plan; the event-driven triggers are not wired yet. |
| `migration.source.env` | map of str → str | `{}` | `MIGRATION_ENV` | Plain configuration for the container. Secret-shaped keys (`*_SECRET`, `*_TOKEN`, `*_KEY`, …) are refused because env values render in clear. |
| `migration.source.secrets` | list of str | `[]` | `MIGRATION_SECRETS` | Environment variable NAMES only. Each value lives in Secrets Manager under `<project>/<environment>/migration/<NAME>`; the plan lists what to create. |
| `migration.target.runtime` | one of: `agentcore`, `ec2` | `'agentcore'` | `MIGRATION_TARGET_RUNTIME` | `agentcore` runs on AgentCore Runtime (arm64, the 8080 `/invocations` contract). `ec2` runs on ECS-on-EC2 inside the platform VPC for amd64-only images; needs `security.networking`. |
| `migration.target.mode` | one of: `adapter`, `native` | `'adapter'` | `MIGRATION_TARGET_MODE` | `adapter` wraps the container so it speaks the AgentCore contract; `native` means the image already does. |
| `migration.network.private_dependencies` | list of str | `[]` | `MIGRATION_PRIVATE_DEPENDENCIES` | Customer-side hostnames the agent must still reach (self-hosted Git, Jira, …). Needs `security.networking` and a `connectivity` other than `none`; the plan prints one reachability check each. |
| `migration.network.connectivity` | one of: `vpn`, `transit-gateway`, `none` | `'none'` | `MIGRATION_CONNECTIVITY` | Path from the platform VPC to the customer network. Usually the longest-lead item in a real migration. |
| `migration.network.dns_forwarders` | list of str | `[]` | `MIGRATION_DNS_FORWARDERS` | IPv4 resolvers for the private hostnames. Unused without `private_dependencies`. |
| `migration.network.ca_bundle_secret_name` | str | `""` | `MIGRATION_CA_BUNDLE_SECRET_NAME` | Secrets Manager NAME of a private CA bundle the agent should trust. Unused without `private_dependencies`. |

## Presets

Each preset is a complete, validated `platform.yaml`; `--profile <name>` copies it for you.

<details>
<summary><code>presets/distributed.yaml</code></summary>

```yaml
# Distributed: every team or workload account runs its own full copy of the
# platform from this one file — auth, gateway, runtimes, all of it. Nothing is
# shared between accounts; organisation-wide guardrails (terraform/org-guardrails)
# are what keep the copies consistent. Pick this when teams must not depend on
# each other's uptime or change windows. See docs/MULTI_ACCOUNT.md.
# Modules: 3 4 5 6 9
project: agentcore-workshop
environment: dev
region: us-east-1
deployment:
  strategy: distributed
identity:
  idp: cognito
agents:
  pattern: orchestrator
gateway:
  web_search: auto
  tools: [sample-tool]
security: {}
observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/federated.yaml</code></summary>

```yaml
# Federated: shared services in a platform account, agents in workload accounts.
# The SAME file deploys into both accounts; the account you deploy into decides
# the role (platform: auth + identity + gateway + observability; workload:
# identity + memory + runtimes + observability). Trust between them is pure
# OAuth — no cross-account IAM on the data plane. See docs/MULTI_ACCOUNT.md.
# Modules: 3 4 5 6 9 (platform side) / 4 6 9 + A (workload side)
#
# Order of operations: deploy in the platform account first, copy the four
# federation values from its outputs into `deployment.federation`, then deploy
# the same file in each workload account. `deploy.sh design` shows which side
# the current credentials will produce.
project: agentcore-workshop
environment: dev
region: us-east-1
deployment:
  strategy: federated
  # Sentinel account ids: `deploy.sh design` refuses to deploy until replaced.
  platform_account: "000000000000"
  workload_accounts: ["123456789012"]
  federation:                      # sentinels — copy from the platform account's `deploy.sh export`
    gateway_url: "https://REPLACE_ME.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    issuer_url: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_REPLACEME"
    m2m_client_id: "REPLACE_ME"
    m2m_client_secret_name: "agentcore/platform-m2m"   # pragma: allowlist secret — a NAME in the workload account
identity:
  idp: entra_id
  tenant_id: "00000000-0000-0000-0000-000000000000"   # sentinel — your Entra tenant id
  client_id: "REPLACE_ME"                             # sentinel — the app registration's client id
  client_secret_name: "agentcore/idp-client-secret"   # pragma: allowlist secret — a NAME, not a value
agents:
  pattern: strands-agent
gateway:
  web_search: auto
  tools: [sample-tool]
security: {}                     # every control opt-in; enable deliberately
observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/greenfield.yaml</code></summary>

```yaml
# Greenfield: first agent platform, one account, defaults everywhere.
# Modules: 3 4 5 6 9
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: centralized

identity:
  idp: cognito

agents:
  pattern: orchestrator

gateway:
  web_search: auto
  tools: [sample-tool]

security: {}          # every control opt-in; enable deliberately

observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/migration.yaml</code></summary>

```yaml
# Migration: an existing agent moves onto the platform; enterprise IdP.
# Modules: 3 4 6 7 9
# Fill in the identity block from your IdP's app registration; the client
# secret goes in Secrets Manager, only its NAME goes here.
# Preview the migration before touching AWS:  deploy.sh migrate plan
project: agentcore-workshop
environment: dev
region: us-east-1
deployment:
  strategy: centralized
identity:
  idp: entra_id
  tenant_id: "00000000-0000-0000-0000-000000000000"   # your Entra tenant
  client_id: "REPLACE_ME"
  client_secret_name: "agentcore/idp-client-secret"  # pragma: allowlist secret — Secrets Manager NAME, not a value
agents:
  pattern: langgraph-agent    # ignored for the image once migration: is set; still selects verify/invoke behaviour
gateway:
  web_search: auto
  tools: [sample-tool]
security: {}                  # target ec2 or private_dependencies need networking: true
observability:
  transaction_search: true
migration:
  source:
    platform: openshift       # where it runs today (shapes the docs, not the deploy)
    build:                    # build here so the image is arm64 (AgentCore Runtime is arm64-only);
      context: ./workshop-simulation/existing-ec2-agent   # use image: for a registry reference instead
      dockerfile: Dockerfile
    port: 8000                # what the container listens on
    invoke_path: /run         # the adapter forwards POST /invocations here
    health_path: /healthz     # ...and GET /ping here
    trigger: webhook          # how work arrives today (webhook|schedule|http|queue)
    env: {}                   # plain config only; secret-looking keys are refused
    secrets: [JIRA_TOKEN, GIT_TOKEN]   # ENV NAMES; values in Secrets Manager under <project>/<env>/migration/<NAME>
  target:
    runtime: agentcore        # ec2 (ECS on EC2 in the VPC) for amd64-only images
    mode: adapter             # native when the image already speaks /invocations + /ping on 8080
  network:
    private_dependencies: []  # customer-side hostnames the agent must reach (needs networking + connectivity)
    connectivity: none        # vpn|transit-gateway once private_dependencies is filled
```

</details>

<details>
<summary><code>presets/multi-agent.yaml</code></summary>

```yaml
# Multi-agent: orchestrator + A2A sub-agents, long-term memory.
# Modules: 3 4 5 6 7 8 9
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: centralized

identity:
  idp: cognito

agents:
  pattern: claude-sdk-multi-agent
  a2a: true
  memory:
    long_term: true
    top_k: 10
    relevance_score: 0.3

gateway:
  web_search: auto
  tools: [sample-tool]

security: {}

observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/platform-team.yaml</code></summary>

```yaml
# Platform team: the full build — every module, distributed-ready.
# Modules: 3 4 5 A 6 7 8 9 C E
# Distributed strategy: each workload team deploys its own copy of this file
# into its own account; org-guardrails (terraform/) apply org-wide once.
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: distributed

identity:
  idp: cognito

agents:
  pattern: orchestrator
  a2a: true
  memory:
    long_term: true

gateway:
  web_search: auto
  tools: [sample-tool]

security:
  networking: true            # VPC mode
  cloudtrail_alerting: true
  traceability: true

observability:
  transaction_search: true
  alarms: true                # CloudWatch alarms + the platform dashboard
```

</details>

<details>
<summary><code>presets/security-focused.yaml</code></summary>

```yaml
# Security-focused: every opt-in control on, Cedar in LOG_ONLY first.
# Modules: 3 4 5 6 9 E
# Flip cedar.mode to ENFORCE only after inspecting decision logs on live
# traffic (docs/SECURITY_CONTROLS.md).
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: centralized

identity:
  idp: cognito

agents:
  pattern: orchestrator
  # Pinned model + allow-list: the runtime roles' Bedrock IAM is scoped to
  # exactly this model, and every agent (including A2A sub-agents) runs it.
  # model_id is required alongside allowed_models — without it the containers'
  # baked-in defaults would silently bypass the list.
  model_id: us.anthropic.claude-sonnet-4-6
  allowed_models:
    - us.anthropic.claude-sonnet-4-6

gateway:
  web_search: auto
  tools: [sample-tool]

security:
  networking: true
  cloudtrail_alerting: true
  resource_policies: true
  egress_filter: true
  require_guardrails: true
  cedar:
    enabled: true
    mode: LOG_ONLY
  traceability: true
  # deploy.sh prompts for the org id, or export ORG_ID — find it with:
  #   aws organizations describe-organization --query Organization.Id
  org_id: ""

observability:
  transaction_search: true
```

</details>

<!-- generated by scripts/gen_platform_reference.py — do not edit by hand -->
