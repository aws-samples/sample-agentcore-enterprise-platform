# Runtime module

A runtime stack (`stacks/runtime_stack.py`) deploys one agent component onto
AgentCore Runtime: it builds the container with CodeBuild, pushes it to a
per-component ECR repository, and creates the `AWS::BedrockAgentCore::Runtime`
with its IAM role, protocol, authorizer, and environment. `app.py`
instantiates it up to three times — the orchestrator, plus the `code-agent`
and `research-agent` A2A sub-agents when A2A is on.

## When it deploys

Per `expected_stacks()` in `infra_utils/platform_config.py`:

| Stack | Condition |
|---|---|
| `{prefix}-runtime-orchestrator` | Every footprint **except a federated platform account** (the platform side runs shared services, no agents) |
| `{prefix}-runtime-code-agent`, `{prefix}-runtime-research-agent` | Additionally requires A2A on |

Dependencies: the orchestrator depends on gateway, memory, and identity
stacks; `research-agent` on gateway and identity (it has web-search tools);
`code-agent` only on auth. In a federated workload account the gateway URL
and issuer come from the platform account's `deployment.federation` block.

## What it creates (per component)

| Resource | Name | Notes |
|---|---|---|
| ECR repository | `{prefix}-{component}` | Scan on push, keep last 10 images, `empty_on_delete`, AES256 (not KMS — see Gotchas) |
| S3 source asset | — | Zip of `agent-code/` (all patterns share one build context; the Dockerfile copies `shared/`) |
| CodeBuild project | `{prefix}-build-{component}` | ARM64, privileged, 30 min timeout; builds `-f <pattern>/Dockerfile`, pushes `<source-hash>` + `latest` tags |
| Build-trigger Lambda + custom resource | `{prefix}-build-trigger-{component}` | Starts a build **only when `SourceHash` changes** — unchanged source is a no-op on `cdk diff` |
| Baseline guardrail (flag) | `{prefix}-{component}-guardrail` | `require_guardrails` only; same `control-library` artifact as the gateway egress filter |
| Runtime IAM role | `{prefix}-{component}-runtime-role` | See Security notes |
| `AWS::BedrockAgentCore::Runtime` | `{project}_{environment}_{component}` (underscores — AgentCore names reject hyphens) | Container URI pinned to the source-hash tag |
| SSM parameters | `/{project}/{environment}/runtimes/{component}/arn`, `.../id` | The published [platform interface](../PLATFORM_INTERFACE.md) |

The source hash (`infra_utils/source_hash.py`) is a SHA-256 over the source
tree **plus the selected Dockerfile pattern** — switching `agent_pattern`
alone changes the tag and triggers a rebuild; without that, the runtime would
keep serving the previous pattern's image.

## Agent patterns and protocols

`agent_pattern` selects which Dockerfile inside `agent-code/` the
orchestrator stack builds. Seven patterns: `orchestrator` (default,
delegating, no tools), `strands-agent`, `langgraph-agent`,
`claude-sdk-agent`, `claude-sdk-multi-agent`, `agui-strands-agent`,
`agui-langgraph-agent`. The A2A sub-agents always build their own
`code-agent` / `research-agent` Dockerfiles.

The protocol comes from `infra_utils/runtime_protocol.py`: `orchestrator` →
`HTTP` (or `AGUI` when the pattern starts with `agui-` — otherwise the
runtime proxies AG-UI's SSE events as plain HTTP), `a2a_agent` → `A2A`,
`mcp_server` → `MCP`.

## Configuration

| platform.yaml | Env var / context flag | Default | Effect |
|---|---|---|---|
| `agents.pattern` | `AGENT_PATTERN` / `agent_pattern` | `orchestrator` | Which Dockerfile the orchestrator builds |
| `agents.a2a` | `ENABLE_A2A` / `enable_a2a` | schema `false`; legacy flag default `true` (deliberately different — see `app.py`) | Deploy the two A2A sub-agent stacks |
| `agents.model_id` | `MODEL_ID` / `model_id` | empty | When set, injected into **every** runtime; when empty, `MODEL_ID` is not injected and each container uses its baked-in `DEFAULT_MODEL_ID` |
| `agents.allowed_models` | `ALLOWED_MODELS` / `allowed_models` | empty | Scopes the role's Bedrock statement; empty keeps the wildcard |
| `security.require_guardrails` | `REQUIRE_GUARDRAILS` / `require_guardrails` | `false` | Baseline guardrail + IAM Null-deny (below) |
| `security.networking` | `ENABLE_NETWORKING` / `enable_networking` | `false` | Runtimes join the VPC in `VPC` network mode |
| `agents.memory.long_term`, `.top_k`, `.relevance_score` | `USE_LONG_TERM_MEMORY`, `LTM_TOP_K`, `LTM_RELEVANCE_SCORE` | `false`, `10`, `0.3` | Passed through to the orchestrator's environment |

## Interfaces

Environment variables injected into the containers (stack-provided first,
`app.py`'s `extra_env_vars` can override):

| Variable | Runtimes | Source |
|---|---|---|
| `PROJECT_NAME`, `ENVIRONMENT`, `COMPONENT_NAME`, `AWS_REGION_NAME`, `SOURCE_HASH` | all | stack |
| `GUARDRAIL_ID`, `GUARDRAIL_VERSION` | all, only with `require_guardrails` | the baseline guardrail |
| `GATEWAY_URL`, `GATEWAY_CREDENTIAL_PROVIDER_NAME` | orchestrator, research-agent | gateway + identity stacks |
| `MEMORY_ID`, `USE_LONG_TERM_MEMORY`, `LTM_TOP_K`, `LTM_RELEVANCE_SCORE`, `STACK_NAME` | orchestrator | memory stack / config |
| `COGNITO_ISSUER_URL`, `COGNITO_ALLOWED_CLIENTS` | orchestrator | `agent-code/shared/auth.py` verifies the caller's JWT against the issuer's JWKS itself, instead of trusting that the runtime authorizer ran |
| `MODEL_ID` | all, only when set | config |

Outputs: `RuntimeArn` (exported as `{prefix}-{component}-runtime-arn`),
`RuntimeId`, `SourceHash`, `ImageUri`, plus the SSM parameters above.

## Security notes

The runtime role is what a compromised agent inherits, so everything that
can be scoped is scoped (each wildcard in the code says why it exists):

| Statement | Scope |
|---|---|
| `ECRPull` | This component's own repository only (used to be `*`) |
| `ECRAuth`, `XRayTracing` | `*` — these actions accept no resource ARN |
| `CloudWatchLogs` (incl. `logs:PutResourcePolicy`) | `/aws/bedrock-agentcore/runtimes/*` — account-wide would let the agent open any log group to another account |
| `CloudWatchMetrics` | `*` with a `cloudwatch:namespace = bedrock-agentcore` condition |
| `BedrockModels` | Wildcard foundation-model + inference-profile ARNs by default; scoped to `agents.allowed_models` when set. A geo-prefixed entry (`us.`…) emits **both** the profile ARN and the base foundation-model ARN — profile-only scoping breaks invokes |
| `SSMAccess` | `parameter/{project}/*` only |
| `AgentCoreAccess` | `*`, **knowingly**: scoping it needs the gateway, memory, and sibling-runtime ARNs, and the A2A targets do not exist yet when the role is built. Tracked as its own task — getting it wrong breaks every live-verified path |
| `AgentCoreIdentityTokenVaultSecrets` | `secretsmanager:GetSecretValue` on `bedrock-agentcore-identity!default/oauth2/*` only — without it the gateway MCP token fetch fails and Strands aborts loading tools |

With `require_guardrails`: `ApplyBaselineGuardrail` allows
`bedrock:ApplyGuardrail` on this stack's guardrail only, and
`DenyUngovernedInference` denies all four inference actions when
`bedrock:GuardrailIdentifier` is **absent** (`Null: true` matches only a
missing key, so no negated-operator guard is needed). Enforcement is IAM,
not convention — a container's baked-in default model without a guardrail is
an `AccessDenied`. See [Security controls](../SECURITY_CONTROLS.md), item 6b.

**JWT authorizer**: client-facing protocols (`HTTP`, `MCP`, `AGUI`) get a
`customJWTAuthorizer` plus `requestHeaderAllowlist: ["Authorization"]` so
agents can read caller JWT claims. The two are gated **together**: the
control plane rejects an Authorization allowlist on a runtime without an
authorizer (observed live on an A2A deploy). A2A runtimes therefore carry no
authorizer and can never read a caller JWT — they are invoked by the
orchestrator, not end users.

## Verification

| Tool | What it proves |
|---|---|
| `scripts/verify.py` | Selects the checks below from the footprint |
| `scripts/invoke.py "<prompt>"` | Live orchestrator invoke (`--agui` for the agui patterns) |
| `scripts/invoke.py --a2a code-agent` / `--a2a research-agent` | Live A2A sub-agent invokes |
| `scripts/check_guardrail_enforcement.py` | IAM policy simulation: no-guardrail invoke denied, with-guardrail allowed |
| `scripts/check_network.py` | Runtimes really are in the VPC when `security.networking` is on |

## Gotchas

- **ECR encryption is immutable** and the repos are custom-named, so
  retrofitting KMS means CloudFormation refuses the replacement (verified —
  it rolls the stack back). Repos stay AES256; a migration is tracked
  separately.
- **`require_guardrails` is incompatible with the claude-sdk patterns** —
  the Claude Agent SDK cannot attach a Bedrock Guardrail, so the IAM deny
  would block every inference. The `platform.yaml` validator refuses the
  combination (raw `-c` users are on their own).
- **`agents.allowed_models` requires `agents.model_id`** — without an
  injected `MODEL_ID`, each container falls back to its baked-in default and
  the allow-list is silently bypassed. The validator enforces this.
- **VPC mode fails fast, never falls back**: asking for `VPC` without
  subnets and security groups raises at synth
  (`infra_utils/runtime_network.py`) instead of quietly deploying `PUBLIC`.
  AgentCore supports only specific AZ *ids* per region, and AZ name→id
  mapping differs per account.
- **A JWT-authorized runtime is invoked with a Bearer token**, not SigV4 —
  use `scripts/invoke.py`. See [`TESTING.md`](../TESTING.md) for the full
  pattern-matrix procedure.
- **Missing source directory synthesizes a placeholder image** (echo-only
  Dockerfile) rather than failing — useful for synth without agent code,
  surprising if you expected an error.
