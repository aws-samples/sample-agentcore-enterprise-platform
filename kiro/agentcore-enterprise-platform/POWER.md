---
name: "agentcore-enterprise-platform"
displayName: "Enterprise Agentic AI Platform Accelerator"
description: "Stand up a governed multi-agent platform on Amazon Bedrock AgentCore using the aws-samples enterprise accelerator — pick a deployment profile, deploy and verify each module, and run a guided team build. Covers Gateway/MCP, A2A agents, memory, identity, networking, security controls, and observability."
keywords: ["agentcore", "enterprise agent platform", "agentcore gateway", "a2a agents", "deployment profile", "agent platform accelerator", "mcp gateway"]
author: "AWS"
---

# Enterprise Agentic AI Platform Accelerator

Drives [`aws-samples/sample-agentcore-enterprise-platform`](https://github.com/aws-samples/sample-agentcore-enterprise-platform)
— a CDK accelerator that deploys a governed agent platform on Amazon Bedrock
AgentCore: Cognito identity, an MCP Gateway with tool targets, agent runtimes,
Memory, A2A sub-agents, VPC networking, security controls, and observability.

Twelve modules, five deployment profiles, one script (`./scripts/deploy.sh`).
This power knows which profile fits a given situation, what each module actually
deploys, which command proves it worked, and which flags are sharp — so a build
session spends its time on the customer's architecture instead of on rediscovery.

## Use this power when someone is

- Standing up an agent platform for an organization, not a single agent
- Choosing between the accelerator's profiles (`greenfield`, `migration`,
  `multi-agent`, `platform-team`, `security-focused`)
- Running or facilitating a guided team build / workshop on this accelerator
- Debugging a module: a deploy that failed, an invoke that returns
  `Unauthorized` or HTTP 424, an agent that reports no tools, traces that never
  appear
- Extending it: adding a gateway tool, contributing a stack through the
  `use-cases/` extension point, swapping the agent framework, turning on
  Cedar / guardrails / VPC mode, going multi-account

## Use something else when

- **Building one agent, locally, from scratch** → the `aws-agentcore` power.
  That one is about `agentcore configure` / `agentcore launch` and the local dev
  loop for a single agent. This one is about a platform that many teams build
  *on*, deployed from an existing CDK repo. If there is no CDK accelerator
  checkout in play, it is probably the other power.
- **General AWS architecture or IaC questions** → a general AWS power.
- The user is asking about Bedrock Agents (the older service) rather than
  Bedrock AgentCore.

## Hard prerequisites

Check these before anything else. Each one has produced a failed session:

| Requirement | Why it is hard |
|---|---|
| `python3.13` on PATH under exactly that name | the scripts invoke `python3.13`, not `python3` |
| `bash` 4+ | the deploy script uses associative arrays; macOS `/bin/bash` is 3.2 and dies on `declare -A`. `brew install bash` |
| `node` + `npm` | the CDK CLI runs through `npx` |
| **A current CDK CLI** — `npm install -g aws-cdk@latest` | `requirements.txt` has no upper bound on `aws-cdk-lib`, and the repo has no `package.json`, so pip installs the newest library while `npx` picks up whatever CLI is installed globally. A stale global CLI fails at bootstrap with a schema-version mismatch, and the prereq check passes it anyway — it only tests that `cdk` exists |
| `aws` CLI with working credentials | validated before any deploy |
| **At least one Claude model enabled in Bedrock, in the target Region** | the first agent invoke (module 6) fails with an access error otherwise |
| A Region where AgentCore *and* the model exist | `us-east-1` is the safe default |
| Docker | **not** required — images build in AWS CodeBuild |

```bash
git clone https://github.com/aws-samples/sample-agentcore-enterprise-platform.git
cd sample-agentcore-enterprise-platform
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./scripts/deploy.sh workshop --dry-run     # zero AWS calls; prints the whole plan
```

Always start with `workshop --dry-run`. It makes no AWS calls at all — no
credential check, no bootstrap — and prints every module, its stacks, and its
verify command. One exception: `--profile security-focused` hits the `ORG_ID` gate
before the plan prints, even in dry-run.

**`--dry-run` only exists for `workshop`.** The `deploy` action parses the flag and
then ignores it: `deploy --module 3 --dry-run` bootstraps the Region and deploys
module 3 for real. Measured — a "dry run" aimed at an untouched Region left a
CDK bootstrap stack and a live Cognito user pool behind. Read `steering/deploy.md`
before previewing anything with `deploy`.

## Pick a profile

The profile decides which modules get walked and which feature flags are on.

| Profile | Modules | For the person who… | Notes |
|---|---|---|---|
| `greenfield` | 3 4 5 6 9 | is building a first agent platform | everything opt-in stays off |
| `migration` | 3 4 6 7 9 | already has an agent and needs it governed | skips module 5, adds tools in 7 |
| `multi-agent` | 3 4 5 6 7 8 9 | needs agents that delegate to each other | `enable_a2a=true` |
| `platform-team` | 3 4 5 A 6 7 8 9 C E | runs the platform other teams build on | networking + security + A2A; **hourly cost** |
| `security-focused` | 3 4 5 6 9 E | has to answer to a security review | needs `ORG_ID`; **hourly cost** |

`security-focused` also enables resource policies, the egress filter, Cedar, and
traceability. The memory resource policy renders `aws:PrincipalOrgID`, so export
an Organizations ID first or the run stops before the first module — including
under `--dry-run`:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

Not in an Organization? Pick another profile, or turn `enable_resource_policies`
off.

## The modules

| # | Module | What lands | Proof | Time |
|---|---|---|---|---|
| **3** | Infrastructure Blueprint | Cognito pool, 3 OAuth clients, SSM discovery params | issuer URL in SSM | ~2 min |
| **4** | Identity Integration | the M2M credential provider agents use to reach the gateway | provider name in SSM | ~2 min |
| **5** | Gateway & Registry | AgentCore MCP Gateway, Lambda tool target, JWT auth | `test_gateway.py` | ~3 min |
| **A** | Memory | managed Memory + user-preference strategy | `test_memory.py` | ~2 min |
| **6** | Agent Deployment | orchestrator agent: CodeBuild → Runtime | `invoke.py` | **~7–8 min** |
| **7** | Gateway Integration | new tool targets, no agent redeploy | `test_gateway.py` | ~3 min |
| **8** | Agent-to-Agent | code + research sub-agents on their own runtimes | `invoke.py --a2a code-agent` | ~8 min |
| **9** | Observability | vended logs, X-Ray, Transaction Search | `check_observability.py` | ~3 min |
| **B** | Code Interpreter | redeploys the orchestrator; no verify, in no profile | — | — |
| **C** | Multi-Account Networking | VPC, private subnets, AgentCore VPC endpoints | `check_network.py` | ~5 min |
| **D** | CI/CD | nothing — a discussion over `.gitlab-ci.yml` | — | — |
| **E** | Security Automation | KMS CMK + CloudTrail | stack COMPLETE | ~3 min |

Roughly 38–40 minutes of deploy time for a full walk. Modules run in the
profile's order, not numeric order.

## Five things that waste the most time

Lead with these. Each is real behaviour, not a caveat.

1. **`--profile` does not scope the deploy — and it writes to disk.** It
   materializes `presets/<profile>.yaml` as `platform.yaml`, the durable
   deployment manifest (`scripts/deploy.sh:107-146`), and then still runs
   `cdk deploy --all` — everything that manifest makes the app synthesize, not
   just the profile's modules. Two consequences worth separating: the *intent*
   now persists across runs, which is the fix; the *scope* still does not narrow,
   which is the trap. Scope with `--module` or `--team`, or use
   `workshop --profile <p>` for the module-by-module path. Interactively you get
   a footprint prompt before `--all` (`confirm_footprint`,
   `scripts/deploy.sh:658-679`) — so the trap is only silent under `--yes` or
   `NON_INTERACTIVE=1`, which is exactly the CI path. Note that `--team platform`
   and `--team security` both fail bare, because `TEAM_MAP` names flag-gated
   stacks — turn networking/security on in the manifest first
   (`facilitation.md`).
2. **`--dry-run` on `deploy` is accepted and ignored.** The `deploy` case never
   checks it (`scripts/deploy.sh:1052-1081` — no `DRY_RUN` test), so
   `deploy --module 3 --dry-run` bootstraps and deploys for real. `--dry-run` is
   honoured only by `workshop`. Confirm from the output, not from what you typed:
   a real dry run prints `DRY RUN — nothing will be deployed` and no
   `═══ Deploying ═══` header. Misspellings, by contrast, are now safe — the
   parser fails closed on an unknown option and on a bad `--profile`/`--team`
   *value* for every action (`scripts/deploy.sh:955-990`), so `--dryrun`,
   `--modul 6` and `--team platfrom` all exit 1 instead of escalating to `--all`.
3. **Module 6 going quiet for 7–8 minutes is not a hang.** CodeBuild is building
   an arm64 container image remotely.
4. **The default `orchestrator` agent has no tools.** Asking it "what tools do
   you have?" correctly returns nothing. Tools live on the gateway — query them
   with `invoke.py --tools` or `test_gateway.py`, or deploy a tool-consuming
   pattern (`strands-agent`, `langgraph-agent`, `claude-sdk-*`, `agui-*`).
   It has **no memory either** — `agent-code/orchestrator/` never reads the
   `MEMORY_ID` it is given, so a same-session recall demo fails even with Memory
   `ACTIVE`. Use `strands-agent` or `langgraph-agent` for a memory demo.
5. **`CREATE_COMPLETE` proves nothing about behaviour.** The verify scripts exist
   because stacks have completed while the thing they promise was broken. Run
   them, and treat a failing verify as information rather than a dead end.

## Runbooks — for doing, not explaining

When the user wants to **run** something rather than understand it, read the
matching runbook and follow it. These are procedures with verification gates and
halt conditions, and they assume a real AWS account and real money.

They are steering files, so read them the same way as any other file below —
`runbook-deploy-platform.md`, not a skill named `deploy-platform`. Kiro's power
installer copies only `POWER.md`, `mcp.json` and `steering/*.md`, so a runbook
that is not a steering file is not a runbook the agent can read.

| Runbook | Use when |
|---|---|
| `runbook-deploy-platform.md` | deploying end to end — a profile walk, a workshop run, a first build |
| `runbook-deploy-module.md` | one module: adding a layer, redoing a failed one, swapping the agent pattern, turning on networking or security |
| `runbook-verify-platform.md` | "is my platform actually working" — the full per-layer pass/fail matrix |
| `runbook-recover-deploy.md` | a module failed, stalled, or was interrupted; a stack is stuck in a rollback state |
| `runbook-cost-audit.md` | "what is this costing" / "did we leave anything running" — read-only, no changes |
| `runbook-teardown-platform.md` | finishing up, or confirming an account is genuinely clean |

All six follow the same discipline: **anything that creates, changes, deletes or
bills goes one command at a time** — state what it does and what it costs, then
stop and wait for approval, and never group it with anything else. Read-only
checks are cheap approvals; group them rather than teaching the participant to
click through five prompts without reading. Never run a billable command to learn
something a read-only call answers.

Two things to say before the first `deploy` of any session, because both are
irreversible once they have happened:

- Once `-auth` exists, **every** `deploy` prints the Cognito M2M client secret to
  stdout in plaintext — including runs that never touch `-auth`. No `deploy` is
  screen-safe. Say so before a shared screen, not after.
- Confirm the account id from `aws sts get-caller-identity` out loud. Deploying
  into the wrong account is the one mistake here with no undo.

## Detailed guidance — read on demand

Read the steering file that matches the question. Do not read them all. The six
`runbook-*.md` files above live here too; these eight are the reference material.

| File | Read this when |
|---|---|
| `deploy.md` | choosing/scoping a deploy, config precedence, `platform.yaml`, feature flags, teams, resume, teardown |
| `modules.md` | you need to know exactly what a module deploys, its stack names, and its verify command |
| `verify.md` | proving a layer works: `deploy.sh verify`, `invoke.py`, `test_gateway.py`, `test_memory.py`, `check_observability.py`, `check_network.py`, the dashboard |
| `troubleshooting.md` | something failed — symptom → cause → fix |
| `agent-patterns.md` | choosing or swapping an agent framework, A2A, AG-UI, protocols, adding a gateway tool |
| `security.md` | a security review: what each control really enforces, SCPs, Cedar, guardrails, VPC mode, identity |
| `facilitation.md` | running a guided team build: agenda, team split, pre-empts, what to say while a module builds |
| `patterns.md` | matching a customer situation to a concrete recipe |

## MCP servers

Two servers ship with this power.

**`agentcore-mcp-server`** (`uvx awslabs.amazon-bedrock-agentcore-mcp-server@latest`)
— AgentCore documentation search plus the AgentCore control plane. This server
has write tools as well as read ones, and **nothing here is pre-approved**: Kiro
deletes `autoApprove` from a power's `mcp.json` on load, so a power cannot grant
itself auto-approval. Every call prompts until the user chooses to always-allow
it. That is the right default — treat the read-only tools below as the ones that
are safe to always-allow, and leave anything that creates, updates, deletes, or
invokes on manual confirmation.

Most useful here:

- `search_agentcore_docs`, `fetch_agentcore_doc` — authoritative service
  behaviour when the accelerator's docs stop short
- `get_runtime_guide`, `get_gateway_guide`, `get_memory_guide`,
  `get_identity_guide`, `get_policy_guide` — deep reference per service
- `list_agent_runtimes`, `get_agent_runtime` — confirm what a deploy produced,
  including `protocolConfiguration` and the runtime version
- `gateway_list`, `gateway_get`, `gateway_target_list` — inspect the gateway and
  its targets
- `memory_list`, `memory_get`, `memory_retrieve_records` — inspect Memory
- `policy_engine_list`, `policy_list` — inspect Cedar policies

**`awsknowledge`** (`https://knowledge-mcp.global.api.aws`) — AWS documentation
for everything around AgentCore: Cognito, CloudFormation, Organizations/SCPs,
X-Ray and Transaction Search, VPC endpoints, Secrets Manager.

Prefer the accelerator's own repo as the source of truth for *its* commands and
flags; use these servers for *service* behaviour. When they disagree about the
accelerator, the checked-out source wins.

## Cost

Real, billable AWS resources. Most of the platform is pay-per-use and nearly
free while idle, with three exceptions worth saying out loud before a session:

- **Networking profiles create a NAT gateway and VPC endpoints that bill
  hourly** — `platform-team` and `security-focused`. Precisely: 1 NAT gateway
  plus 5 interface endpoints across 2 AZs, so 10 endpoint-AZ-hours. The other
  three profiles leave **nothing** billing hourly. `deploy.md` has the full
  standing-cost inventory, which is the answer to "what does this cost if we
  leave it up overnight?"
- **Transaction Search changes span-ingestion pricing account-wide**, and it
  defaults **on** (`enable_transaction_search`). It stays enabled after teardown
  on purpose, because other workloads may come to rely on it. Deploy with
  `-c enable_transaction_search=false` if a platform team owns tracing
  elsewhere — but expect no traces, which is exactly the failure it prevents.
- CodeBuild runs per container build.

Tear down the same day: `./scripts/deploy.sh destroy`. The networking stack can
refuse to delete for a few hours while AgentCore's network interfaces drain —
expected, and the expensive parts (NAT, endpoints) are already gone by then.

## Never do these

- Put a secret in `platform.yaml`, `workshop.env`, or a CDK context flag. The
  accelerator passes Secrets Manager **names** only, resolved at deploy time.
- Assume the Cognito M2M client secret is only in Secrets Manager. The `-auth`
  stack exports it to CloudFormation **in plaintext**, and once that stack exists
  **every** later `deploy` prints it to stdout — including runs that never touch
  `-auth`, because the closing summary dumps every prefix-matching stack's outputs.
  No `deploy` is screen-safe. It is also the first thing a security reviewer can
  confirm in one read-only call; `security.md` has the mechanism and the mitigations.
- Set `cedar_mode=ENFORCE` before reading the `LOG_ONLY` decision logs. The
  shipped permit is unconstrained on principal and resource; narrow it first.
- Attach the Terraform SCPs anywhere but a sandbox OU on the first pass. They
  are additive-deny and applied from the Organizations management account.
- Describe `enable_networking=true` as air-gapped. Private subnets keep a NAT
  route; see `security.md`.

---

This power ships inside the accelerator it drives, at
`kiro/agentcore-enterprise-platform/`. Every `file:line` citation, profile
sequence and `--flag` below is checked against the surrounding source tree by
`scripts/check-kiro-power.sh` on every pull request, so these files move when the
code moves instead of drifting from a pinned commit. MCP tool names validated
against `amazon-bedrock-agentcore-mcp-server` 1.29.0. Where a command here and
the source still disagree, the source wins — please open an issue.
