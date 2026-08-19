# Participant Guide

You are going to deploy a working agent platform on Amazon Bedrock AgentCore,
one layer at a time, and prove each layer works before moving on. Two days at
a walking pace; a determined afternoon if you skip the discussion.

If something breaks, go straight to [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) —
it is organised by symptom and every entry is a failure that really happened.

---

## Before you start

**Accounts and access.** One AWS account you can create IAM roles, Cognito
pools, ECR repositories, CodeBuild projects, and Bedrock AgentCore resources
in. A sandbox or dev account is ideal. The deploy script validates your
credentials before it touches anything.

**Bedrock model access.** Enable at least one Claude model in the Bedrock
console for your region *before* module 6, or the first agent invoke fails with
an access error.

**Local tools.**

| Tool | Required? | Note |
|---|---|---|
| `python3.13` | Yes | exactly this name on PATH, not `python3` |
| `node` + `npm` | Yes | the CDK CLI runs through npx |
| `aws` CLI | Yes | configured with working credentials |
| `bash` 4+ | Yes | macOS ships 3.2 — `brew install bash` |
| Docker / finch | **No** | images build in AWS CodeBuild |

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Region.** Pick one where AgentCore and your chosen model both exist;
`us-east-1` is the safe default. Some features are region-gated (the built-in
web-search gateway connector, for one) and switch themselves off elsewhere
rather than failing.

**Cost.** Most of the platform is pay-per-use and nearly free while idle. Two
exceptions worth knowing: profiles with networking create a **NAT gateway and
VPC endpoints that bill hourly**, and enabling Transaction Search changes
span-ingestion pricing **account-wide**. Tear down when you are done.

---

## Two ways to run it

**Guided (recommended for a first pass).** Explains each module, deploys it,
verifies it, and waits for you:

```bash
./scripts/deploy.sh workshop                      # greenfield profile
./scripts/deploy.sh workshop --dry-run            # see the whole plan, no AWS calls
./scripts/deploy.sh workshop --from 6             # resume where you stopped
```

Start with `--dry-run`. It prints every module, the stacks it will deploy, and
the command that will verify it, without touching your account.

**Direct.** When you know what you want:

```bash
./scripts/deploy.sh deploy --module 5             # one module's stacks
./scripts/deploy.sh deploy --team agent           # one team's stacks
./scripts/deploy.sh deploy --profile greenfield   # the profile's feature flags, all stacks
```

One sharp edge: `--profile` sets the profile's **feature flags** and then
deploys *everything the app defines* (`cdk deploy --all`) — not just the
modules in that profile's guided sequence. Use `workshop --profile …` if you
want the module-by-module path, or `--module` / `--team` to scope a direct
deploy.

---

## Pick your profile

The profile decides which modules you walk and which features are on.

| Profile | Modules | You are the person who… |
|---|---|---|
| `greenfield` | 3 4 5 6 9 | is building a first agent platform |
| `migration` | 3 4 6 7 9 | has an agent already and needs it governed |
| `multi-agent` | 3 4 5 6 7 8 9 | needs agents that delegate to each other |
| `platform-team` | 3 4 5 A 6 7 8 9 C E | runs the platform other teams build on |
| `security-focused` | 3 4 5 6 9 E | has to answer to a security review |

`platform-team` and `security-focused` both turn on VPC networking and the
security stack (see the cost note). Everything opt-in stays off unless the
profile or your `platform.yaml` asks for it.

**`security-focused` needs an AWS Organizations ID.** It also enables resource
policies, the egress filter, Cedar, and traceability — and the resource
policies render `aws:PrincipalOrgID`, so the deploy stops early without it:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

If your account is not in an Organization, pick another profile or turn
`enable_resource_policies` off.

---

## The modules

Each one deploys real infrastructure and then proves it works. Times are from
real runs — the long ones are container builds in CodeBuild, not hangs.

| # | Module | What lands | Proof it worked | Time |
|---|---|---|---|---|
| **3** | Infrastructure Blueprint | Cognito user pool, OAuth clients (app/web/m2m), SSM discovery parameters | issuer URL readable from SSM | ~2 min |
| **4** | Identity Integration | The M2M credential provider agents use to reach the gateway. IdP federation and 3LO providers are **opt-in** (see note) | credential provider name in SSM | ~2 min |
| **5** | Gateway & Registry | AgentCore MCP Gateway, Lambda tool target, JWT auth against Cognito | `test_gateway.py` — real `tools/list` + `tools/call` | ~3 min |
| **A** | Memory | Managed Memory, user-preference strategy (semantic extraction only with long-term memory on) | `test_memory.py` | ~2 min |
| **6** | Agent Deployment | The orchestrator agent: CodeBuild builds the container, Runtime runs it | `invoke.py` — the agent answers | **~7–8 min** |
| **7** | Gateway Integration | New tool targets, picked up without redeploying the agent | `test_gateway.py` shows the new tool | ~3 min |
| **8** | Agent-to-Agent | code + research sub-agents on their own runtimes, serving the A2A JSON-RPC contract | `invoke.py --a2a code-agent` — a real A2A invoke | ~8 min |
| **9** | Observability | Vended log delivery, X-Ray tracing, Transaction Search | `check_observability.py` | ~3 min |
| **B** | Code Interpreter | *(no guided narration or verify — not in any profile sequence; redeploys the orchestrator)* | — | — |
| **C** | Multi-Account Networking | VPC, private subnets, AgentCore VPC endpoints | `check_network.py` | ~5 min |
| **E** | Security Automation | KMS CMK encryption + CloudTrail audit logging | stack reaches COMPLETE | ~3 min |
| **D** | CI/CD (discussion) | nothing — read `.gitlab-ci.yml` as the reference | — | — |

Things that look wrong and are not:

- **Module 6 takes minutes with little output.** CodeBuild is building an arm64
  container image remotely. Expect ~7 minutes on a first build.
- **In `platform-team`, Memory (A) comes before Agent Deployment (6)** on
  purpose. The orchestrator depends on memory, so if 6 ran first CDK would
  create memory implicitly and module A would report "no changes" — which
  would teach you something false about what you just built.
- **Module 4 deploys less than its title suggests, by default.** The gateway
  M2M credential provider is always created. Enterprise IdP federation only
  happens if you chose one (`IDP_TYPE=entra_id|okta|ping`; the prompt defaults
  to plain Cognito), and the Google/GitHub/Notion 3LO providers only when you
  supply their client ids. Nothing silently half-configures.
- **The default orchestrator agent has no tools.** It is a deliberately minimal
  agent, so asking it "what tools do you have?" gets you nothing. Tools live on
  the gateway — query them with `invoke.py --tools` or `test_gateway.py`, or
  deploy a pattern that consumes them (`strands-agent`, `langgraph-agent`, the
  `claude-sdk-*` and `agui-*` patterns).

---

## Verifying, beyond the module checks

The guided run verifies each module for you. These are the same tools by hand:

```bash
python scripts/test.py                       # platform health: token, invoke, resources
python scripts/invoke.py "Reply with exactly: WORKSHOP OK"
python scripts/invoke.py --tools             # tools registered on the GATEWAY (asks the gateway, not the agent)
python scripts/invoke.py --agui "…"          # for agui-* patterns (typed SSE events)
python scripts/invoke.py --a2a code-agent "…"  # A2A sub-agents (JSON-RPC, SigV4)
python scripts/test_gateway.py               # gateway directly: tools/list + tools/call
python scripts/check_observability.py        # logs + traces are actually accepted
python scripts/check_observability.py --spans  # …and spans are searchable (needs a recent invoke)
```

`--tools` and `test_gateway.py` talk to the gateway with a machine token — they
tell you what is *registered*, which is not the same question as what a given
agent *loaded*. To see the agent's own view, ask a tool-using pattern to list
its tools (the default orchestrator has none).

**A verify failing is useful information, not a dead end.** The guided run
offers to continue; if you say yes, note which module it was — the later
modules build on it, and the failure usually explains a stranger symptom two
modules later.

There is also a local dashboard (status only, runs on your machine):

```bash
python dashboard/monitor.py &
python3 -m http.server 8888 -d dashboard/public
```

---

## Choosing an agent framework

The platform is framework-agnostic; the agent pattern is a config value, not a
rewrite. Seven patterns ship, all verified live:

`orchestrator` (default) · `strands-agent` · `langgraph-agent` ·
`claude-sdk-agent` · `claude-sdk-multi-agent` · `agui-strands-agent` ·
`agui-langgraph-agent`

```bash
AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6
```

The `agui-*` patterns speak the AG-UI protocol (typed SSE events for building
a UI) and must be invoked with `invoke.py --agui`. A bad pattern name stops the
deploy immediately rather than failing later in CodeBuild.

---

## Describing your deployment in one file

Instead of environment variables, you can declare everything in
`platform.yaml` — project, region, multi-account strategy, IdP, agent pattern,
memory, gateway tools, security controls:

```yaml
project: acme-agents
region: us-east-1
deployment:
  strategy: centralized      # centralized | distributed | federated
agents:
  pattern: langgraph-agent
security:
  networking: true
```

Copy a starting point from `presets/` (one per profile) and validate it offline
before deploying — it reports every problem at once:

```bash
python -m infra_utils.platform_config platform.yaml
./scripts/deploy.sh config          # what is actually in effect, and from where
```

Secrets never go in this file: it holds Secrets Manager *names*, never values.
For the multi-account strategies, see [`MULTI_ACCOUNT.md`](MULTI_ACCOUNT.md).

---

## Tearing down

```bash
./scripts/deploy.sh destroy                  # everything
./scripts/deploy.sh destroy --stack <name>   # one stack, refuses if others depend on it
```

Two expected annoyances, both explained in
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md): a targeted destroy **refuses**
rather than cascading into dependent stacks (that is the safety feature), and
the networking stack can fail to delete for a few hours while AgentCore's
network interfaces drain. Retry later; the expensive parts (NAT, endpoints) are
already gone by then.

Transaction Search stays enabled after teardown, on purpose — it is an
account-level setting other workloads may now rely on.

---

## Where to read more

| Question | Document |
|---|---|
| How does it fit together? | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| How is the caller authenticated? | [`IDENTITY.md`](IDENTITY.md) |
| What security controls exist, and what do they really enforce? | [`SECURITY_CONTROLS.md`](SECURITY_CONTROLS.md) |
| One account or many? | [`MULTI_ACCOUNT.md`](MULTI_ACCOUNT.md) |
| Why don't I see traces? | [`TRACING.md`](TRACING.md) |
| How do I test all of this properly? | [`TESTING.md`](TESTING.md) |
| It broke. | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) |
