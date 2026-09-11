# The modules

Read this when you need to know exactly what a module deploys, which stack
carries it, what it publishes for other modules to find, and the command that
proves it worked.

`PREFIX` throughout is `${PROJECT_NAME}-${ENVIRONMENT}`, defaulting to
`agentcore-workshop-dev`. Verify commands run from the repo root.

Every Python command here is spelled `.venv/bin/python`, which is what the guided
run itself uses (`MODULE_VERIFY` at `scripts/deploy.sh:190-199`). Prefer it over a
bare `python` even in an activated shell: `source .venv/bin/activate` does not
survive between an agent's tool calls, so a bare `python` fails with
`ModuleNotFoundError: boto3` for reasons that look nothing like the real cause.

---

## Map at a glance

| # | Module | Stacks | Verify | Time |
|---|---|---|---|---|
| 3 | Infrastructure Blueprint | `$PREFIX-auth` | SSM issuer URL | ~2 min |
| 4 | Identity Integration | `$PREFIX-auth` `$PREFIX-identity` | SSM credential provider name | ~2 min |
| 5 | Gateway & Registry | `$PREFIX-gateway` | `test_gateway.py` | ~3 min |
| 6 | Agent Deployment | `$PREFIX-runtime-orchestrator` | `invoke.py` | **~7–8 min** |
| 7 | Gateway Integration | `$PREFIX-gateway` | `test_gateway.py` | ~3 min |
| 8 | Agent-to-Agent | `$PREFIX-runtime-code-agent` `$PREFIX-runtime-research-agent` | `invoke.py --a2a code-agent` | ~8 min |
| 9 | Observability | `$PREFIX-observability` | `check_observability.py` | ~3 min |
| A | Memory | `$PREFIX-memory` | `test_memory.py` | ~2 min |
| B | Code Interpreter | `$PREFIX-runtime-orchestrator` | *(none)* | — |
| C | Multi-Account Networking | `$PREFIX-networking` | `check_network.py` | ~5 min |
| D | CI/CD | *(none — exits 0)* | *(none)* | — |
| E | Security Automation | `$PREFIX-security` | stack COMPLETE | ~3 min |

Modules 5 and 7 are the **same stack**. So are 6 and B. That is intentional: 7
grows the gateway by redeploying it with more targets, and B adds the code
interpreter by redeploying the orchestrator.

`--module D` prints a message pointing at `.gitlab-ci.yml` and exits 0 — there
is nothing to deploy. An unknown module id exits 1 with the valid list.

**Three modules live behind feature flags, and only one of them turns its own flag
on.** This bites on the standalone `deploy --module` path, which is what people use
to redo a single module:

| Module | Flag | App default | Bare `deploy --module` |
|---|---|---|---|
| 8 | `enable_a2a` | **`true`** (`app.py:83`) | works |
| C | `enable_networking` | `false` | **fails**: `No stacks match the name(s) …-networking` |
| E | `enable_security` | `false` | **fails**: `No stacks match the name(s) …-security` |

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module C
ENABLE_SECURITY=true   ./scripts/deploy.sh deploy --module E
```

The guided loop re-exports `ENABLE_A2A=true` for module 8
(`scripts/deploy.sh:901`) so a profile that set it false does not break the walk;
there is no equivalent for C or E. Inside `workshop --profile platform-team` or
`security-focused` the flags are already set, so this only affects standalone runs.
The error's own advice — "Check CloudFormation console for details" — is a dead end:
the stack was never synthesized.

---

## Module 3 — Infrastructure Blueprint

**Stack:** `$PREFIX-auth`

Cognito User Pool with email sign-in and three OAuth app clients — `app`, `web`,
`m2m` — plus the `agentcore/invoke` resource-server scope. This is the trust root
for everything else: every AgentCore call in this platform is authenticated.

**Publishes to SSM** under `/{project}/{env}/auth/`:
`issuer-url`, `user-pool-id`, `app-client-id`, `web-client-id`, `m2m-client-id`.

**Stack outputs** (verified on a real deploy — nine named): `UserPoolId`,
`UserPoolArn`, `IssuerUrl`, `DiscoveryUrl`, `DomainUrl`, `IdPType`,
`AppClientId`, `WebClientId`, `M2MClientId`.

**Plus four CDK auto-generated `ExportsOutput…` entries, and one of them is the
M2M client secret in plaintext.** Not a typo and not a flag you can turn off — see
`security.md`. The practical consequence: **once this stack exists, every later
`deploy.sh deploy` prints a live secret to stdout**, because the script's closing
summary enumerates every stack matching the project prefix and dumps its outputs
regardless of what you deployed (`scripts/deploy.sh:659`). Verified with
`deploy --module C`, which does not touch `-auth` at all. Keep it off a shared
screen and out of pasted logs.

```bash
aws ssm get-parameter --name "/$PROJECT_NAME/$ENVIRONMENT/auth/issuer-url" \
  --region "$AWS_REGION" --query Parameter.Value --output text

# The outputs, without printing the secret value:
aws cloudformation describe-stacks --stack-name "$PREFIX-auth" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[].{Key:OutputKey,Len:length(OutputValue)}' --output table
```

---

## Module 4 — Identity Integration

**Stacks:** `$PREFIX-auth`, `$PREFIX-identity`

The one thing this always creates is the **`gateway-m2m` OAuth2 credential
provider** in the AgentCore Identity Token Vault. That is what lets a runtime
fetch its own gateway token instead of being handed one.

**It deploys less than its title suggests, by default.** Say this before someone
concludes it is broken:

- Enterprise IdP federation happens only if you chose one
  (`IDP_TYPE=entra_id|okta|ping`); the prompt defaults to plain Cognito.
- The Google / GitHub / Notion 3LO providers appear only when you supply their
  client ids — **and a secret *name* alongside each one.** A client id with no
  `<vendor>_client_secret_name` is a hard failure at synth
  (`stacks/identity_stack.py:51-62`), and the plaintext
  `<vendor>_client_secret` / `<VENDOR>_CLIENT_SECRET` keys are rejected outright
  (`app.py:166-174`). The error prints the `create-secret` command and the `-c`
  key to pass, and `scripts/deploy.sh` does both for you when the secret is in
  the environment. So the shape is:

  ```bash
  aws secretsmanager create-secret --name agentcore-workshop-dev-google-oauth-secret \
    --secret-string '<client-secret>'
  # then: -c google_client_secret_name=agentcore-workshop-dev-google-oauth-secret
  ```

  If you are looking at an older deployment: before this was fixed, every 3LO
  provider synthesized with an empty `Oauth2ProviderConfigInput` because the
  config dict's top-level key did not match the CloudFormation model, and the L1
  mapping dropped the whole block silently. They could not have worked. A
  redeploy on current `main` is the fix, not a configuration change.

Nothing silently half-configures. Full Entra ID walkthrough is in the repo at
`docs/ENTERPRISE_IDP.md`, including how to verify federation without opening a
browser.

**Publishes to SSM:** `/{project}/{env}/identity/gateway-credential-provider-name`,
and `/{project}/{env}/identity/{provider}-provider-arn` per 3LO provider.

```bash
aws ssm get-parameter --name "/$PROJECT_NAME/$ENVIRONMENT/identity/gateway-credential-provider-name" \
  --region "$AWS_REGION" --query Parameter.Value --output text
```

---

## Module 5 — Gateway & Registry

**Stack:** `$PREFIX-gateway`

The AgentCore MCP Gateway with `CUSTOM_JWT` auth against the Cognito issuer, and
a Lambda tool target. Agents discover tools through the gateway instead of
hardcoding endpoints, and every call is auditable.

Ships `sample-tool` (Lambda, `tools/sample_tool/`) exposing
`text_analysis_tool`. In Regions where it is supported, also the built-in
`web-search` connector. Tools are named `<target>___<tool>` — e.g.
`sample-tool___text_analysis_tool`. That full name is what Cedar policies and
agent prompts refer to.

Opt-in on this stack: the Cedar policy engine (`enable_cedar`) and the Bedrock
Guardrail + egress interceptor Lambda (`enable_egress_filter`).

**Publishes to SSM:** `/{project}/{env}/gateway/url`.

```bash
.venv/bin/python scripts/test_gateway.py          # tools/list + one real tools/call
```

---

## Module A — Memory

**Stack:** `$PREFIX-memory`

AgentCore managed Memory with a **user-preference strategy**. Semantic fact
extraction is added only when `use_long_term_memory=true` — it costs more, so it
is off by default. `ltm_top_k` defaults to 10 and `ltm_relevance_score` to 0.3.

Opt-in: KMS CMK encryption, and the in-account-only resource policy
(`enable_resource_policies`, needs `org_id`).

**Publishes to SSM:** `/{project}/{env}/memory/memory-id`,
`/{project}/{env}/memory/memory-arn`.

```bash
.venv/bin/python scripts/test_memory.py
```

Note what this does *not* prove: `test_memory.py` uses **your local
credentials**, so it passes regardless of whether the runtime role can reach
Memory. See `troubleshooting.md` for how to check the role itself.

**Ordering:** in `platform-team`, A runs before 6 on purpose — the orchestrator
depends on memory, so running 6 first would make CDK create memory implicitly
and module A would then report "no changes."

---

## Module 6 — Agent Deployment

**Stack:** `$PREFIX-runtime-orchestrator`

The orchestrator agent on AgentCore Runtime. CodeBuild builds an **arm64**
container image remotely — no local Docker — and `CfnRuntime` runs it. Image
tags are a content hash of the source plus the selected pattern, so CodeBuild
only reruns when something actually changed.

**This is the slow one: ~7–8 minutes with almost no output on a first build.**
That is the remote container build, not a hang.

The runtime receives `MODEL_ID`, `GATEWAY_URL`,
`GATEWAY_CREDENTIAL_PROVIDER_NAME`, `MEMORY_ID`, and — for client-facing
protocols — `COGNITO_ISSUER_URL` and `COGNITO_ALLOWED_CLIENTS` (injected by
`app.py`, not by the runtime stack).

**Publishes to SSM:** `/{project}/{env}/runtimes/{component}/arn` and
`/{project}/{env}/runtimes/{component}/id`.

```bash
.venv/bin/python scripts/invoke.py "Reply with exactly: WORKSHOP OK"
```

**Bedrock model access must be enabled in this Region before this module**, or
the first invoke fails with an access error. If `MODEL_ID` is unset, each
pattern falls back to its own in-code default; a dated model id that has aged
out into Legacy status is rejected in fresh accounts, so override it:

```bash
export MODEL_ID=us.anthropic.claude-sonnet-5
```

The default `orchestrator` pattern has **no tools** and extracts no caller
identity. Both are deliberate.

---

## Module 7 — Gateway Integration

**Stack:** `$PREFIX-gateway` (redeployed with more targets)

This is the module where the platform stops being something you deployed and
becomes something you extend. **Agents pick up new tools on their next
discovery, with no agent redeploy.** That is the claim worth demonstrating live.

Two kinds of target:

| | Built-in connector | Lambda target |
|---|---|---|
| You write | nothing | a handler + tool schema |
| Good for | capabilities AWS operates (web search) | your APIs, data, business logic |
| Credentials | the gateway's IAM role | whatever your Lambda needs |
| Example | `web-search` in `stacks/gateway_stack.py` | `sample-tool` in `tools/sample_tool/` |

Three things that are easy to get wrong — all in `docs/GATEWAY_TARGETS.md`:

1. **Connector config must go through `add_property_override`**, not
   `target_configuration`. The L1 construct predates connector targets and
   silently drops the key; the target then deploys with no connector and the tool
   never appears.
2. **The gateway role needs the connector's own action.** Web search needs
   `bedrock-agentcore:InvokeWebSearch` on
   `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1` — note the literal
   `aws` where an account id would normally be. Without it the target deploys and
   every call fails at invoke time.
3. **Connectors are regional.** Gate any connector you add the way `app.py`
   gates web search.

For a Lambda tool, the tool name arrives in the **context**, not the event:
`context.client_context.custom["bedrockAgentCoreToolName"]`, formatted
`<target>___<tool>`. Split on `___` and dispatch on the suffix. Return
`{"content": [{"type": "text", "text": ...}]}` or `{"error": "..."}`. Declare
the schema in `app.py`'s `tool_configs` using **PascalCase** keys (`Name`,
`Description`, `InputSchema`, `Type`, `Properties`, `Required`) — that is the
CloudFormation shape, not MCP JSON.

Write the tool `Description` for a model, not for a human skimming a table. It is
the only thing the agent has when deciding whether the tool answers the question.

```bash
./scripts/deploy.sh deploy --module 7
.venv/bin/python scripts/test_gateway.py
.venv/bin/python scripts/invoke.py --tools        # your tool should be listed
```

---

## Module 8 — Agent-to-Agent (A2A)

**Stacks:** `$PREFIX-runtime-code-agent`, `$PREFIX-runtime-research-agent`

Two specialized sub-agents on their own runtimes, each with independent auth,
scaling, and lifecycle. The orchestrator delegates to them.

The A2A stacks only exist in the CDK app when `enable_a2a=true`, so
**`--module 8` exports `ENABLE_A2A=true` automatically.**

The sub-agents speak **JSON-RPC 2.0 on port 9000**, not the HTTP payload shape.
Their contract, served by `agent-code/shared/a2a_serve.py`:

- `POST /` — JSON-RPC
- `GET /.well-known/agent-card.json`
- `GET /ping` returning `{"status": "Healthy"}`

all on `0.0.0.0:9000`.

**Two different failures get conflated here, including by the accelerator's own
docs.** `docs/TROUBLESHOOTING.md` ("Invoke returns HTTP 424") and the docstring at
`scripts/invoke.py:112` both say that sending `{"prompt": ...}` to an A2A runtime
"gets you a 424." Measured against a working `code-agent`, it does not:

| What is wrong | What you actually see |
|---|---|
| **Client** sends the wrong envelope (`{"prompt": …}` instead of JSON-RPC `message/send`) | **HTTP 200** with a JSON-RPC error body: `"code":-32600`, `"message":"Request payload validation error"`, and a pydantic `missing field: method` detail |
| **Container** serves the wrong protocol (built on `BedrockAgentCoreApp`/HTTP 8080 instead of JSON-RPC 9000) | **HTTP 424**, with clean container logs |

So a 200 is not success here — read the body. And 424 means the *image* is wrong,
which is a redeploy, not a payload fix. Searching the repo docs for "424" after an
envelope mistake sends you down the wrong path entirely; that mismatch is real
enough that `tests/test_a2a_contract.py` exists to guard the container side of it
statically.

They are also guarded differently: **A2A runtimes use SigV4, not a JWT.** A2A is
not a client-facing protocol, so those runtimes deliberately get no JWT
authorizer and rely on IAM `InvokeAgentRuntime`. `scripts/invoke.py` picks the
right mechanism per target, which is why it is the recommended path.

```bash
.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"
.venv/bin/python scripts/invoke.py --a2a research-agent "…"
```

---

## Module 9 — Observability

**Stack:** `$PREFIX-observability`

Vended log delivery plus X-Ray tracing for the gateway, memory, and runtimes —
and the two settings that make tracing work at all: a CloudWatch Logs resource
policy allowing X-Ray to write span log groups, and a trace segment destination
of `CloudWatchLogs`.

**Without that destination change, every OTLP span batch is rejected with HTTP
400 and the deployment still reports success.** This is why
`enable_transaction_search` defaults to `true`. It is **account- and
Region-scoped**, it changes span-ingestion pricing account-wide, and destroying
this stack does **not** revert it — other workloads may have come to depend on
it.

Opt-in: `enable_traceability` adds SNS + EventBridge alerting on sensitive
AgentCore API calls. It needs CloudTrail management events, so enable it together
with `enable_security` (module E).

```bash
.venv/bin/python scripts/check_observability.py            # destination + policy + deliveries
.venv/bin/python scripts/check_observability.py --spans    # …and spans are searchable
```

`--spans` is deliberately **not** part of module 9's verify: span delivery lags
an invocation by a minute or two. Run it later, after some invokes.

**Expect the verify to fail the first time in a new account.** Turning Transaction
Search on is asynchronous and outlasts the stack that requests it, and the verify
runs about a second after `CREATE_COMPLETE` with no retry:

```
FAIL: trace segment destination is CloudWatchLogs but status is PENDING
```

Measured on a fresh account: **8m22s** from `CREATE_COMPLETE` to `ACTIVE`. Poll
`aws xray get-trace-segment-destination`, then re-run the verify — it passes
unchanged. Only the first enablement per account is slow. Because module 9 is last
in `greenfield`, this lands on the final step of a guided session; say it in advance.

---

## Module B — Code Interpreter

**Stack:** `$PREFIX-runtime-orchestrator` (redeploy)

Adds the sandboxed code interpreter to the orchestrator. It has **no guided
narration and no verify command, and appears in no profile's sequence** — it is
available via `--module B` but is not part of any standard walk. Budget a full
container rebuild for it, same as module 6.

---

## Module C — Multi-Account Networking

**Stack:** `$PREFIX-networking`

VPC, private subnets, and AgentCore VPC endpoints. When `enable_networking=true`,
runtimes get `network_mode: VPC` with the private subnets and a runtime security
group — passing nothing is what would otherwise make "enterprise network
isolation" untrue.

**Publishes to SSM:** `/{project}/{env}/networking/vpc-id`,
`/{project}/{env}/networking/private-subnet-ids`,
`/{project}/{env}/networking/runtime-security-group-id`.

**38 resources, ~3m30s.** Six endpoints get created: `bedrock-agentcore.gateway`,
`bedrock-runtime`, `ecr.api`, `ecr.dkr`, `logs` (interface) and S3 (gateway).

The endpoint policy needs `org_id`. Without it the endpoint is created with **no
policy at all** — a warning, not an error. And with it, only the AgentCore endpoint
is restricted; the other five keep the wide-open AWS default. See `security.md` for
what that policy does and does not cover.

**This module does not put your agents in the VPC.** Existing runtimes stay
`networkMode: PUBLIC` until redeployed — measured, with `check_network.py
--expect-public` passing while the networking stack was `CREATE_COMPLETE`. Finish
the job:

```bash
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module 6   # 345s
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module 8   # 172s
```

**The AZ trap, which is real and costs ~15 minutes:** AgentCore supports a limited
set of AZ **ids** per Region, `max_azs=2` takes the first two AZ *names*
alphabetically, and name → id mapping differs per account. On a fresh `us-east-1`
account this landed a subnet in `use1-az6`, which AgentCore does not support; the
stack still reached `CREATE_COMPLETE`, and the runtime redeploy is what failed. The
fix is a **source edit** (there is no flag) plus a **destroy and recreate** (an
in-place AZ change collides on subnet CIDRs). Full recipe and measurements in
`security.md`.

```bash
aws cloudformation describe-stacks --stack-name "$PREFIX-networking" \
  --region "$AWS_REGION" --query "Stacks[0].StackStatus" --output text | grep -q COMPLETE \
  && .venv/bin/python scripts/check_network.py
```

`check_network.py` stops at the first failure, so expect to see the AZ problem and
the placement problem one after the other, not together.
`check_network.py --expect-public` asserts the opposite posture, for confirming a
non-VPC deployment is genuinely public rather than accidentally so.

**Teardown warning:** AgentCore leaves `agentic_ai` ENIs behind for up to ~8
hours after runtimes stop using the VPC, and they block subnet/security-group
deletion. NAT and endpoints delete normally, so waiting costs nothing meaningful.
If no runtime ever successfully entered the VPC there are no ENIs and the stack
deletes cleanly first time — measured at 4m32s.

---

## Module D — CI/CD

No stacks. `--module D` logs a pointer to `.gitlab-ci.yml` as the reference
implementation and exits 0. This is a discussion module: read the pipeline, map
it onto the customer's own CI, talk about what gates a promotion.

---

## Module E — Security Automation

**Stack:** `$PREFIX-security`

KMS customer-managed key encryption and CloudTrail audit logging. It is the
prerequisite for `enable_traceability` (module 9's alerting), because that rule
only fires if CloudTrail management events are being recorded.

Verify is a stack-status check — there is no behavioural probe here:

```bash
aws cloudformation describe-stacks --stack-name "$PREFIX-security" \
  --region "$AWS_REGION" --query "Stacks[0].StackStatus" --output text | grep -q COMPLETE
```

Which is exactly why `security.md` exists: reaching `COMPLETE` tells you the
resources were created, not that any control is enforcing anything.

---

## The SSM registry is the extension seam

Every stack publishes its outputs under `/{project}/{env}/*`. That registry is
how the stacks find each other, and it is the clean place for customer-specific
extensions to read platform values without importing CDK constructs or
hardcoding ARNs:

```
/{project}/{env}/auth/issuer-url | user-pool-id | app-client-id | web-client-id | m2m-client-id
/{project}/{env}/identity/gateway-credential-provider-name
/{project}/{env}/identity/{provider}-provider-arn
/{project}/{env}/gateway/url
/{project}/{env}/memory/memory-id | memory-arn
/{project}/{env}/runtimes/{component}/arn | id
/{project}/{env}/networking/vpc-id | private-subnet-ids | runtime-security-group-id
```

Capture the whole set at once:

```bash
./scripts/deploy.sh export      # → workshop-outputs-<stamp>.json
```

**Use the `ssm_parameters` half and ignore `stack_outputs`.** The SSM collection is
a single API call and is correct — 18 parameters on a full platform-team deploy, and
none of them is a secret. `stack_outputs` is always garbage: the script merges each
stack's JSON by splitting on whitespace and re-parsing the fragments, so the
documents are shredded into single characters (`stack_outputs` came back as a list
of 769 one-character strings, with every `OutputKey` lost). It fails silently under
`except: pass`. If you need stack outputs, query CloudFormation directly.

The file is written to the repo root and is **not** in `.gitignore`. Harmless today
only because the broken merge drops the `-auth` secret — see `security.md`.
