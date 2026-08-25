# Situation → recipe

Read this when someone describes their situation and you need to turn it into a
concrete sequence of commands rather than a tour of the options.

Each recipe assumes: repo root, `.venv` active, `AWS_REGION` and credentials set.
`PREFIX` is `${PROJECT_NAME}-${ENVIRONMENT}`.

Jump table:

| They said | Recipe |
|---|---|
| "We're building our first agent platform" | [First platform](#first-platform) |
| "We already have an agent and need it governed" | [Govern an existing agent](#govern-an-existing-agent) |
| "We need agents that call other agents" | [Delegating agents](#delegating-agents) |
| "We're the platform team; other teams build on us" | [Platform for other teams](#platform-for-other-teams) |
| "We have to get through a security review" | [Security review](#security-review) |
| "We use LangGraph / CrewAI / our own framework" | [Framework swap](#framework-swap) |
| "We log in with Entra ID / Okta / Ping" | [Corporate IdP](#corporate-idp) |
| "Many agent teams, one governed tool catalogue" | [Central tool governance](#central-tool-governance) |
| "Nothing may reach the internet" | [No internet egress](#no-internet-egress) |
| "Prove our tenants are isolated" | [Tenant isolation](#tenant-isolation) |
| "We need end-to-end traces" | [End-to-end traces](#end-to-end-traces) |
| "Add our internal API as a tool" | [Add an internal API as a tool](#add-an-internal-api-as-a-tool) |
| "Agents must only call approved tools" | [Deny tools by policy](#deny-tools-by-policy) |
| "Half a day, minimal spend" | [Cheapest useful demo](#cheapest-useful-demo) |
| "What would production actually need?" | [The production gap](#the-production-gap) |

---

## First platform

**They have no agent in production and want the governed shape from day one.**

Profile `greenfield` (3 4 5 6 9), ~18 minutes of deploy. Networking, security and
A2A all off — deliberately, so the first pass is about the trust root, the tool
gateway, and one working agent.

```bash
export AWS_REGION=us-east-1
export MODEL_ID=us.anthropic.claude-sonnet-5
./scripts/deploy.sh workshop --dry-run --profile greenfield
./scripts/deploy.sh workshop --profile greenfield
```

Then prove it. One command covers the whole footprint and exits non-zero if any
part of it is broken:

```bash
./scripts/deploy.sh verify
```

Run the individual tools when you need to show the room *which* layer works, or
when `verify` fails and you want to isolate it — each answers a different question:

```bash
.venv/bin/python scripts/test_gateway.py                           # gateway serves tools
.venv/bin/python scripts/invoke.py "Reply with exactly: PLATFORM OK"
.venv/bin/python scripts/invoke.py --tools                          # registered on the GATEWAY, not the agent
.venv/bin/python scripts/check_observability.py
```

**Where the conversation should go next:** module 7 (add their own tool) and a
pattern swap. Those two are what make it their platform rather than a sample.

**Do not** reach for `platform-team` because it sounds more complete. It turns on
networking, which bills hourly, and adds 20 minutes before anyone sees an agent
answer.

---

## Govern an existing agent

**They have a working agent on EC2, ECS, Lambda, or a laptop, and the ask is
governance — auth, an audited tool path, observability — not a rewrite.**

Profile `migration` (3 4 6 7 9). Note it **skips module 5** and reaches the
gateway through module 7 instead. That ordering is the argument: an existing agent
gets governed by adding **tool targets**, not by rebuilding the agent.

```bash
./scripts/deploy.sh workshop --profile migration
```

The sequence to walk with them:

1. **3 + 4** — their agent now has an issuer to validate tokens against and a
   credential provider to fetch its own gateway token from. Nothing about their
   agent code changed yet.
2. **6** — lift the agent onto Runtime. Their framework stays; the pattern is a
   config value (see [Framework swap](#framework-swap)).
3. **7** — move each capability the agent calls directly into a gateway target.
   This is the step that produces the audit trail.
4. **9** — traces across the whole path.

The honest version of the migration cost: their agent code has to (a) read
identity from the verified caller token rather than trusting a header, and
(b) call tools through the gateway's MCP client instead of direct SDK calls.
`agent-code/langgraph-agent/` and `agent-code/strands-agent/` are the two
worked examples — point at the code, not at a diagram.

`workshop-simulation/` in the repo carries an end-to-end version of exactly this
story, including a pre-existing EC2-style agent to migrate.

---

## Delegating agents

**One agent cannot reasonably own everything — they want specialists.**

Profile `multi-agent` (3 4 5 6 7 8 9), ~29 minutes.

```bash
./scripts/deploy.sh workshop --profile multi-agent
.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"
.venv/bin/python scripts/invoke.py --a2a research-agent "Summarise what you can do."
```

The architectural point to make, and it is a real one: the sub-agents get
**independent auth, scaling, and lifecycle**. The code-agent can be
rate-limited, redeployed, or IAM-restricted without touching the orchestrator.
That is not true of a single agent with many tools.

Two facts that shape their design:

- Sub-agents are guarded by **IAM/SigV4**, not a caller JWT — A2A is not a
  client-facing protocol, so those runtimes get no JWT authorizer at all. The
  boundary between "our agents" and "our users" is therefore a real boundary.
- The contract is **JSON-RPC 2.0 on port 9000** plus an agent card and `/ping`.
  Anything they write must serve all three or it returns HTTP 424.

Details in `agent-patterns.md`.

**When to argue against it:** if the sub-agents would share one execution role and
one deployment cadence, they are getting the complexity of A2A and none of the
isolation. Tools on the gateway are the simpler answer.

---

## Platform for other teams

**They are a central team and their customer is other engineering teams.**

Profile `platform-team` (3 4 5 A 6 7 8 9 C E), ~39 minutes. Networking and
security on — check the cost conversation first.

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
./scripts/deploy.sh workshop --profile platform-team
```

The three things this profile is actually demonstrating:

1. **The SSM registry is the product interface.** Their teams read platform values
   from `/{project}/{env}/*` instead of importing CDK constructs or hardcoding
   ARNs. That is the seam that lets the platform change underneath consumers:

   ```
   /{project}/{env}/auth/issuer-url | user-pool-id | app-client-id | web-client-id | m2m-client-id
   /{project}/{env}/identity/gateway-credential-provider-name
   /{project}/{env}/gateway/url
   /{project}/{env}/memory/memory-id | memory-arn
   /{project}/{env}/runtimes/{component}/arn | id
   /{project}/{env}/networking/vpc-id | private-subnet-ids | runtime-security-group-id
   ```

2. **`--team` splits ownership.** `platform` owns networking/auth/identity/gateway/
   observability; `agent` owns the runtimes and memory; `security` owns the
   security and observability stacks.

3. **A targeted `destroy` refuses rather than cascading.** That refusal is the
   multi-tenant safety property — one team cannot take the platform out from under
   another.

Note the ordering: **A (Memory) runs before 6.** The orchestrator depends on
memory, so running 6 first makes CDK create memory implicitly and module A then
reports "no changes," teaching the room something false.

If they have many agent teams and want one governed tool catalogue, go to
[Central tool governance](#central-tool-governance).

---

## Security review

**There is a security or risk function that has to sign off.**

Profile `security-focused` (3 4 5 6 9 E). It needs an Organizations id and
enables resource policies, the egress filter, Cedar, and traceability. Note it
sets `enable_a2a=false` on purpose — this profile is about the control plane, not
the agent fleet.

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
./scripts/deploy.sh workshop --profile security-focused
```

**Lead with the disclosure, not the feature list.** The credibility of this
accelerator in a review comes from saying these first:

- Every security control is **off by default**.
- **`CREATE_COMPLETE` proves nothing is being enforced.** Module E's verify is a
  stack-status check because there is no behavioural probe.
- Cedar ships in **`LOG_ONLY`** with one permit that is unconstrained on principal
  and resource. It is not a default-deny gateway until three separate things
  change.
- The egress filter **masks; it rarely blocks**, does no authorization, passes
  unrecognised payload shapes through unchanged, and has no defined
  fail-open/fail-closed behaviour.
- The VPC endpoint policy's org restriction covers **SigV4 callers only** — JWT
  callers carry no IAM principal.
- Two `iam.*` library files are **reference policies nothing deploys**.
- The runtime role still holds `bedrock-agentcore:*` on `Resource: "*"`,
  knowingly.

Then show the mechanism, which is genuinely strong:

```bash
make validate-controls && make test-controls

# Prove a control is real without deploying: flag off → absent, flag on → present
cdk synth $PREFIX-gateway                        | grep -c PolicyEngine
cdk synth $PREFIX-gateway -c enable_cedar=true   | grep -c PolicyEngine
```

And the org layer, **attached to a sandbox OU first**:

```bash
cd terraform/org-guardrails
terraform init
terraform plan  -var 'target_ids=["ou-sandbox-xxxx"]'
terraform apply -var 'target_ids=["ou-sandbox-xxxx"]'
```

Full control inventory, what each really enforces, and the SCP quota mechanics are
in `security.md`. The single sharpest control to show a reviewer is
`scp.identity.deny-workload-token-for-userid` — it closes an API that takes the
user identifier as an unverified string.

---

## Framework swap

**"Does this work with the framework we already use?"**

This is the claim the whole pitch rests on, and it is one command:

```bash
AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6
.venv/bin/python scripts/invoke.py "Reply with exactly: LANGGRAPH LIVE"
```

One orchestrator redeploy, ~5–8 minutes of CodeBuild, no infrastructure change. A
bad pattern name is rejected before any AWS call.

Seven patterns ship: `orchestrator`, `strands-agent`, `langgraph-agent`,
`claude-sdk-agent`, `claude-sdk-multi-agent`, `agui-strands-agent`,
`agui-langgraph-agent`. The `agui-*` ones need `invoke.py --agui`.

**If their framework is not in the list**, the extension is small and worth
showing rather than promising:

1. Add `agent-code/<their-pattern>/` with a `Dockerfile` — the build runs
   `docker build --platform linux/arm64 -f <pattern>/Dockerfile .` from
   `agent-code/`, so `shared/` is in the build context. Anything importing
   `shared/` must `COPY shared/ shared/`.
2. Add the name to `AGENT_PATTERNS` in `scripts/deploy.sh` (the up-front
   validation list).

That is it — no stack changes. A directory named `agui-*` is automatically given
the `AGUI` protocol; everything else gets `HTTP`.

Prove a swap actually happened rather than trusting a green deploy — the image tag
is a content hash, so identical tags mean CodeBuild never rebuilt:

```bash
aws ecr describe-images --repository-name $PREFIX-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-5:].{tags:imageTags,pushed:imagePushedAt}'
```

If they want the full matrix before committing, `agent-patterns.md` has it.

---

## Corporate IdP

**"Our people log in with Entra ID / Okta / Ping."**

Module 4 handles it, but **only if a federated IdP was chosen** — the prompt
defaults to plain Cognito, and nothing silently half-configures.

```bash
export IDP_TYPE=entra_id           # or okta | ping
export IDP_TENANT_ID=<tenant>      # Entra
export IDP_CLIENT_ID=<app-client-id>
./scripts/deploy.sh deploy --module 4
```

The client secret is prompted for interactively (never echoed, never persisted to
`workshop.env`), stored in Secrets Manager as `$PREFIX-idp-client-secret`, and only
the **name** is passed to CDK. Override the name with `IDP_CLIENT_SECRET_NAME` to
use a secret they already own.

One detail that costs an hour when it goes wrong: **the script strips whitespace
from the pasted secret.** A trailing newline from a copy-paste produces
`invalid_client` at the IdP, which looks like a misconfiguration rather than a
paste error.

Cognito stays in the picture as the token issuer the platform validates against —
federation means their IdP authenticates the human and Cognito issues the token
the platform checks. The full Entra ID walkthrough, including verifying federation
without opening a browser, is `docs/ENTERPRISE_IDP.md` in the repo.

Separately, the Google / GitHub / Notion **3LO** providers appear only when their
client ids are supplied. Those are for agents acting on a user's behalf against a
third-party API — a different thing from workforce login. Do not conflate them in
the same slide.

---

## Central tool governance

**Many agent teams, and the tool catalogue must be governed centrally.**

`deployment.strategy: federated` in `platform.yaml`. One platform account owns the
gateway, tools, and Cognito; workload accounts own their runtimes and their own
memory.

```yaml
project: acme-agents
environment: prod
region: us-east-1
deployment:
  strategy: federated
  federation:
    gateway_url: <platform gateway url>
    issuer_url: <platform cognito issuer>
    m2m_client_id: <platform m2m client id>
    m2m_client_secret_name: <secrets manager name in THIS account>
```

```bash
.venv/bin/python -m infra_utils.platform_config platform.yaml   # validate offline first
```

Three properties worth naming, all confirmed in the code:

- **The same `platform.yaml` deploys both sides.** The account you deploy from
  decides the role. Platform side gets auth/identity/gateway/observability and
  **no runtimes**; workload side gets identity/memory/runtimes/observability.
  Deploying from an account in neither list fails at synth with a message naming
  both.
- **Federated trust is pure OAuth — no cross-account IAM on the data plane.** The
  workload account's own credential provider holds the platform Cognito M2M client
  id and secret; the token vault exchanges them at the platform token endpoint; the
  platform gateway validates the resulting JWT against its own issuer and
  **cannot tell which account called it**. That last part is either the feature or
  the objection, depending on the customer — surface it rather than waiting.
- **Memory stays per workload account**, on purpose. Conversation history is the
  tenant boundary and account isolation is the strongest wall available.

An incomplete `federation` block raises a `ValueError` naming all four required
keys, so a partial config fails before any deploy.

If instead each team wants a full independent copy, that is
`strategy: distributed`. Compare all three in `deploy.md`.

---

## No internet egress

**"Nothing in this VPC may reach the internet."**

Give the honest answer first: **`enable_networking=true` is not that.** It creates
private subnets with a **NAT gateway** and a route to the internet — deliberately,
because AgentCore ENIs in a public subnet get no internet route at all and the
runtimes would lose Bedrock access. There is no flag that turns this into an
air-gapped VPC.

What the platform does give them, and it is not nothing:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module C     # ~3m30s, 38 resources
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module 6     # ~6m — REQUIRED
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module 8     # ~3m, if A2A is deployed
.venv/bin/python scripts/check_network.py
```

- runtime security group with **no inbound rules** and egress restricted to
  **TCP 443 only**
- interface endpoints for Bedrock Runtime, ECR API, ECR Docker, CloudWatch Logs,
  and the AgentCore Gateway, plus a free S3 **gateway** endpoint — which also keeps
  image-layer pulls off NAT data processing
- an org-scoped endpoint policy on the AgentCore endpoint — **but only for SigV4
  callers**; JWT callers carry no `aws:PrincipalOrgID`

**Three traps, all measured, all of which have bitten a real run:**

1. **Module C alone proves nothing.** Runtimes stay `networkMode: PUBLIC` until
   redeployed — which is why modules 6 and 8 are in the sequence above and not
   optional. `check_network.py --expect-public` *passes* in the half-done state.
2. **`enable_networking=true` without `ORG_ID` does not fail.** It warns and creates
   the endpoint with **no policy at all**. Treat that warning as an error.
3. **`ORG_ID` restricts one endpoint out of six.** `bedrock-runtime`, `ecr.*`,
   `logs` and S3 keep the wide-open AWS default policy. Do not let "we set `ORG_ID`"
   stand as an answer about the model-invocation path.

**Budget for the AZ trap before you promise a timeline.** AgentCore only supports
some AZ *ids*, `max_azs=2` picks AZ *names*, and the mapping is per-account — so
whether this works is an accident of the account. On a fresh `us-east-1` account it
failed: `CREATE_COMPLETE` networking stack, then the runtime redeploy rejected with
`subnets are in unsupported availability zones`. Fixing it needs a **source edit**
(no flag exists) and a **destroy + recreate** (an in-place AZ change collides on
subnet CIDRs), which is about **15 minutes**. Run `check_network.py` in the target
account *before* the session — it is a two-minute pre-flight that turns a live
detour into a footnote.

The real no-egress design is a follow-on engagement: an endpoint for every
dependency, no NAT gateway, and a validated list of what the chosen agent framework
calls at runtime. Say that plainly — it is a credible next step, not a gap in the
sample.

If the requirement is really "tools must not reach public endpoints," the
control they want is the org SCP `scp.gateway.targets-require-private-endpoint`,
which is a different and much cheaper answer. See `security.md`.

---

## Tenant isolation

**"Show us that one customer's data cannot reach another's."**

Three layers, and it is worth walking all three because each fails differently:

1. **Identity.** The verified `sub` claim from the caller's JWT becomes the
   AgentCore Memory `actor_id`. Every failure path in
   `agent-code/shared/auth.py` **raises** — there is no fallback to an unverified
   decode and no placeholder identity, precisely because a defaulted identity would
   file two callers under one actor and mix their history.

   The asymmetry to disclose: a missing issuer is a hard reject, but an **empty**
   `COGNITO_ALLOWED_CLIENTS` degrades quietly to "any client of the correct
   issuer." Set both.

2. **Resource policy.** `enable_resource_policies=true` puts an
   `AWS::BedrockAgentCore::ResourcePolicy` on memory: allow this account, deny
   anything outside the org. Requires `org_id`; the stack raises rather than
   deploying a policy with a hole in it.

3. **Account.** For hard multi-tenancy, `strategy: federated` keeps **memory per
   workload account**. Account isolation is the strongest wall available and the
   reason memory is not centralised.

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
./scripts/deploy.sh deploy --module A -c enable_resource_policies=true
.venv/bin/python scripts/test_memory.py
```

**Be careful what you claim from that last command.** `test_memory.py` uses **your
local credentials**, so it passes regardless of whether the *runtime role* can
reach memory. It is not an isolation proof. To check the role:

```bash
aws iam list-role-policies --role-name <project>-orchestrator-role
```

What is genuinely missing today, and worth saying: the platform gives per-actor
memory scoping and account-level separation, but the orchestrator's role holds
`bedrock-agentcore:*` on `Resource: "*"` — so isolation between tenants **inside
one account** rests on the agent code passing the right `actor_id`, not on IAM.
For a strict tenancy requirement, one account per tenant is the defensible answer.

---

## End-to-end traces

**"We need to see a request across the agent, the gateway, and the model."**

```bash
./scripts/deploy.sh deploy --module 9
.venv/bin/python scripts/check_observability.py
.venv/bin/python scripts/invoke.py "hi"
sleep 120
.venv/bin/python scripts/check_observability.py --spans
```

Disclose the cost mechanic **before** deploying: `enable_transaction_search`
defaults to **true**, is **account- and Region-scoped**, changes span-ingestion
pricing account-wide, and **survives teardown** by design because other workloads
may come to depend on it. If that setting is not theirs to change:

```bash
ENABLE_TRANSACTION_SEARCH=false ./scripts/deploy.sh deploy --module 9
```

…and say plainly that tracing then does not work: every OTLP span batch is
rejected with HTTP 400 while the deploy still reports success. That failure mode is
the best possible argument for why the flag defaults on — a stack that says
`CREATE_COMPLETE` while silently discarding all telemetry.

**Do not let anyone "fix" empty `aws xray batch-get-traces` results.** With
Transaction Search all spans are searchable in `aws/spans`, while the classic X-Ray
APIs only serve the indexed sample (default rule: 1%). An empty trace-API result is
expected.

---

## Add an internal API as a tool

**"How do we expose our own service to the agents?"**

The strongest demonstrable claim in the whole accelerator: **agents pick up new
tools on their next discovery, with no agent redeploy.** Show it in that order.

1. Write the handler in `tools/<my_tool>/handler.py`. The tool name arrives in the
   **context**, not the event:

   ```python
   def handler(event, context):
       delimiter = "___"
       name = context.client_context.custom["bedrockAgentCoreToolName"]
       name = name[name.index(delimiter) + len(delimiter):]
       if name == "my_tool":
           return {"content": [{"type": "text", "text": do_the_thing(event["some_arg"])}]}
       return {"error": f"Unsupported tool: {name}"}
   ```

2. Declare it in `app.py`'s `tool_configs` beside `sample-tool`, using
   **PascalCase** keys (`Name`, `Description`, `InputSchema`, `Type`,
   `Properties`, `Required`) — that is the CloudFormation shape, not MCP JSON.

3. Deploy and verify:

   ```bash
   ./scripts/deploy.sh deploy --module 7
   .venv/bin/python scripts/test_gateway.py
   .venv/bin/python scripts/invoke.py --tools          # the new tool is listed
   ```

4. **Now the point** — an agent that was deployed *before* the tool existed uses
   it:

   ```bash
   .venv/bin/python scripts/invoke.py "Use my_tool on '…' and report what it returns."
   ```

Two things to tell them before they write it:

- **Write the tool `Description` for a model, not for a human skimming a table.**
  It is the only thing the agent has when deciding whether this tool answers the
  question. "The agent never calls our tool" is usually a vague description — or
  the `orchestrator` pattern, which has no tools at all.
- **Their tool needs a Cedar permit** the moment `cedar_mode=ENFORCE`. The action
  name is the full `<TargetName>___<tool_name>`.

If what they want is a capability AWS operates rather than their own code, the
built-in connector path is different and shorter — but connectors are **regional**
and need their own IAM action on the gateway role. Both traps are in
`agent-patterns.md`.

---

## Deny tools by policy

**"Agents must only be able to call approved tools, and we need the evidence."**

Cedar, rolled out in the only safe order:

```bash
# 1. Attach in LOG_ONLY (the default) and generate real traffic
./scripts/deploy.sh deploy --module 5 -c enable_cedar=true
.venv/bin/python scripts/test_gateway.py
.venv/bin/python scripts/invoke.py "Use the text analysis tool on 'hello world'."

# 2. Read the decision logs. Confirm what WOULD have been denied.
# 3. Narrow the shipped permit's principal and resource:
#    control-library/cedar/gateway-default/10-permit-read-tools.cedar
# 4. Only then enforce:
./scripts/deploy.sh deploy --module 5 -c enable_cedar=true -c cedar_mode=ENFORCE
```

What to tell them up front:

- **Cedar is implicit default-deny** — no permit means denied. That is the
  property they want, and it is real.
- The shipped permit is **unconstrained on principal and resource**, so out of the
  box any authenticated caller may invoke the sample tool on any gateway. Narrow it
  before enforcing.
- **Never add a blanket `forbid`.** In Cedar a matching forbid overrides every
  permit unconditionally — it would make the permits dead code and deny everything.
  Keep any forbid narrow.
- **Every new tool is denied under `ENFORCE` unless a permit names it.** Correct
  behaviour, and the most common "we broke the demo" moment. Build it into their
  tool-onboarding checklist.
- Flags match the exact lowercase string `"true"`. `-c enable_cedar=True` silently
  does nothing.

At org scope, the matching control is
`scp.gateway.require-policy-engine`, which denies creating a gateway *without* a
Cedar engine in `ENFORCE`. That is how a central team makes this
non-optional — see `security.md`, including the fact that it deploys merged with
seven other gateway SCPs into one policy.

---

## Cheapest useful demo

**Half a day, someone else's account, and no appetite for surprise spend.**

```bash
export AWS_REGION=us-east-1
export MODEL_ID=us.anthropic.claude-sonnet-5
./scripts/deploy.sh workshop --dry-run --profile greenfield     # free
./scripts/deploy.sh workshop --profile greenfield
```

`greenfield` sets `enable_networking=false`, which is the whole cost story: **no
NAT gateway, no interface endpoints, nothing billing hourly.** What remains is
pay-per-use and near-free idle, plus CodeBuild minutes per container build.

Three decisions to make explicitly:

- **`--profile` alone deploys every stack the manifest declares**, not just the
  profile's modules. Use `workshop --profile`, or scope with `--module`. For
  `greenfield` the overshoot is cheap — 6 stacks, the extra one being Memory —
  and the manifest does keep networking out of the app. What you lose is the
  module-by-module walk and the verify step between layers. Note `--profile`
  writes `platform.yaml`, so it is not a read-only way to look: use `--dry-run`,
  or read `expected_stacks()` off the preset (`deploy.md`).
- **In someone else's account, `deploy --dry-run` is the trap that ruins the
  "no surprise spend" promise.** Only `workshop` honours `--dry-run`; `deploy`
  accepts it and deploys. Measured: `deploy --module 3 --dry-run` bootstrapped a
  fresh Region and left a Cognito user pool standing. If the command you are about
  to run to "just show them the plan" says `deploy`, it is not a preview.
- **`enable_transaction_search` defaults true** and is account-scoped. In someone
  else's account, either get agreement or run module 9 with
  `ENABLE_TRANSACTION_SEARCH=false` and say what that costs you.

Capture, then destroy the same day:

```bash
./scripts/deploy.sh export        # workshop-outputs-<stamp>.json — contains account ids
./scripts/deploy.sh destroy
```

If you never enabled networking there is no ENI drain to wait out, which is the
other reason `greenfield` is the right demo profile.

---

## The production gap

**"What else would we need before this is production?"** Worth having ready,
because answering it well is more persuasive than the demo.

The accelerator is deliberately a starting point. The gaps that are real, in
roughly the order they bite:

| Gap | Why it matters | Where to start |
|---|---|---|
| Controls are off by default | the shipped defaults are a working platform, not a hardened one | `security.md` |
| Cedar permits are unconstrained | narrow principal + resource, then `ENFORCE` | [Deny tools by policy](#deny-tools-by-policy) |
| Guardrail resolves to `DRAFT` | pin a published version | `security.md` |
| Runtime role holds `bedrock-agentcore:*` on `*` | scope it once the gateway/memory/A2A ARNs exist | `security.md` |
| Egress interceptor has no fail-open/fail-closed decision | a Bedrock throttle surfaces as a Lambda failure | `security.md` |
| CloudTrail is single-Region, bucket auto-deletes | do not present it as replacing an org trail | `security.md` |
| SNS alert topic has no subscriber and no CMK | alerts go nowhere by default | `security.md` |
| `iam.*` control-library files are reference-only | nothing deploys them | `security.md` |
| CI/CD is a reference pipeline, not a deployment | module D deploys nothing on purpose | `--module D` |
| No multi-Region story | the accelerator is single-Region | — |

Framing that holds up: **every one of these is disclosed in the repo rather than
hidden**, which is the strongest signal about the accelerator's quality that you
can give a security-minded customer. Lead with the list, not with the demo.

---

*Recipes here are grounded in the accelerator's source. Where a recipe states a
timing or a failure signature, it came from a real run — see the pinned commit in
`POWER.md`. Anything not confirmed either way is left out rather than hedged.*
