<!-- markdownlint-disable MD041 -->

# Memory module

The memory module (`stacks/memory_stack.py`) creates the platform's AgentCore
Memory store: conversation history that survives across sessions, partitioned
by caller. The partition key (`actor_id`) is the verified `sub` claim from the
caller's JWT — the memory store is where the platform's identity verification
pays off, because an unverified identity would mix different callers' history
under one actor ([Verify caller identity](../IDENTITY.md)). Short-term memory
(raw conversation events) is always on; long-term memory (semantic fact
extraction) is opt-in because it incurs additional inference cost.

## When it deploys

| Configuration | Deployed? |
|---|---|
| Centralized / distributed (the default) | Always |
| Federated, platform account | No — the platform account runs no agents, so it needs no memory |
| Federated, workload account | Yes — **each workload account gets its own memory**, deliberately: conversation history is the tenant boundary, and account isolation is the strongest wall available |

Source: `expected_stacks()` in `infra_utils/platform_config.py` adds
`{prefix}-memory` when the federated role is not `platform`; `app.py` mirrors
the condition.

## What it creates

| Resource | Name pattern | Purpose |
|---|---|---|
| `AWS::BedrockAgentCore::Memory` | `{project}_{environment}_memory` (hyphens become underscores) | The memory store; events expire after 30 days |
| Memory strategy: `user_preference` | `{name}_user_pref`, namespace `USER_ID` | Always — user preference tracking |
| Memory strategy: `semantic` (optional) | `{name}_semantic`, namespace `AGENT_ID` | Only with `use_long_term_memory=true` — semantic fact extraction |
| `AWS::BedrockAgentCore::ResourcePolicy` (optional) | attached to the memory ARN | Only with `enable_resource_policies=true` — in-account-only access (see Security notes) |
| 2 × `AWS::SSM::Parameter` | `/{project}/{environment}/memory/*` | `memory-id`, `memory-arn` |

## Configuration

Precedence everywhere: cdk context > env var > `platform.yaml` > default.

| cdk context | Env var | `platform.yaml` | Default | Effect |
|---|---|---|---|---|
| `use_long_term_memory` | `USE_LONG_TERM_MEMORY` | `agents.memory.long_term` | `false` | Adds the semantic strategy |
| `ltm_top_k` | `LTM_TOP_K` | `agents.memory.top_k` | `10` | Long-term retrieval: max records (1–100) |
| `ltm_relevance_score` | `LTM_RELEVANCE_SCORE` | `agents.memory.relevance_score` | `0.3` | Long-term retrieval: relevance threshold (0.0–1.0) |
| `enable_resource_policies` | `ENABLE_RESOURCE_POLICIES` | `security.resource_policies` | `false` | Attaches the in-account-only resource policy |
| `org_id` | `ORG_ID` | `security.org_id` | `""` | Required when resource policies are on; synth fails without it |
| `enable_security` | `ENABLE_SECURITY` | `security.cloudtrail_alerting` | `false` | Indirect: when the security stack exists, its KMS CMK becomes the memory's `encryption_key_arn` |

Two things the table implies but are worth stating: `event_expiry_days` is a
stack parameter but `app.py` hardcodes it to `30` — it is not configurable
today. And the two `ltm_*` retrieval knobs shape nothing in this stack; they
are consumed by the agent at runtime (injected as env vars into the
orchestrator by `app.py`), not by the memory resource itself.

## Interfaces

SSM parameters under `/{project}/{environment}/memory/` (part of
[the platform interface](../PLATFORM_INTERFACE.md)):

| Parameter | Value |
|---|---|
| `memory-id` | The AgentCore Memory id |
| `memory-arn` | The memory ARN |

Stack outputs `MemoryId` / `MemoryArn` are also **exported** as
`{project}-{environment}-memory-id` / `-memory-arn` — the only module of the
three with CloudFormation exports. External consumers should still use the SSM
parameters; exports are not part of the platform interface. `app.py` injects
`MEMORY_ID` (plus `USE_LONG_TERM_MEMORY`, `LTM_TOP_K`, `LTM_RELEVANCE_SCORE`)
into the orchestrator runtime's environment.

## Security notes

- **Encryption at rest with a CMK is opt-in**, and rides on the security stack
  (`security.cloudtrail_alerting` / `ENABLE_SECURITY`): when that stack
  exists, its KMS key ARN is passed as `encryption_key_arn`; otherwise the
  memory uses the service default. Enforcing CMK-only memory across an
  organization is an SCP (`control-library/scp/memory/enforce-cmk.json`,
  applied via `terraform/org-guardrails/`), not something this stack does —
  see [Security controls](../SECURITY_CONTROLS.md) and
  [TESTING.md](../TESTING.md) items B2/B4.
- The optional resource policy
  (`control-library/resource-policies/memory/in-account-only.json`) allows
  `bedrock-agentcore:*` on the memory for the account root and denies any
  principal outside the AWS Organization (`aws:PrincipalOrgID`), with a
  `aws:ViaAWSService: false` guard so service-linked access keeps working.
- Data-plane access comes from the **runtime role**
  (`stacks/runtime_stack.py`): `CreateEvent`, `GetEvent`, `ListEvents`,
  `DeleteEvent`, `ListSessions`, `RetrieveMemoryRecords` and more — currently
  on `Resource: "*"`, knowingly, because scoping needs ARNs that do not exist
  when the role is built; tracked as its own task (see the code comment).
- Tenant isolation inside one memory is `actor_id` — application-level, backed
  by the JWT verification in `agent-code/shared/auth.py`. Account isolation in
  a federation is the harder boundary.

## Verification

`scripts/verify.py` runs `scripts/test_memory.py` whenever memory is in the
footprint: it creates real conversation events, lists them, fetches one by id,
exercises pagination, and confirms an invalid memory id is rejected — five
checks against the live data plane, non-zero exit on failure. The orchestrator
invoke (`scripts/invoke.py`) then exercises the same memory through the agent
path.

## Gotchas

- **Memory is per-workload in a federation — by design, not omission.** There
  is no shared memory in the platform account; if two workload accounts need
  shared history, that is an architecture decision to make explicitly, not a
  flag to flip ([Multi-account federation](../MULTI_ACCOUNT.md)).
- Framework memory integrations need more than `CreateEvent`: LangGraph's
  checkpointer lists events to rehydrate a thread, which is why the runtime
  role carries `ListEvents`/`ListSessions`/`DeleteEvent`. A pattern that
  worked on one framework can fail at invoke on another — the
  [pattern matrix](../TESTING.md) exists to catch exactly this.
- The memory **name** uses underscores (`{project}_{environment}_memory`)
  while every other resource uses hyphens — AgentCore Memory naming rules,
  worth knowing before you grep for it.
- Enabling `enable_resource_policies` without `org_id` fails at synth with a
  clear message; a placeholder org id (`o-REPLACEME`) is rejected by
  `platform.yaml` validation before it can render into a policy that matches
  nothing.
- Long-term memory extraction runs additional inference — the flag is off by
  default for cost, not capability, reasons.
