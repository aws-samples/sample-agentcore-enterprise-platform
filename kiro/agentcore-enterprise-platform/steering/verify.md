# Verifying

Read this when you need to prove a layer works — or prove it does not.

**A stack reaching `CREATE_COMPLETE` proves nothing about behaviour.** These
scripts exist because stacks have completed successfully while the thing they
promise was broken. Runtimes emitted spans for weeks while X-Ray rejected every
batch. Run the checks.

All commands from the repo root with `.venv` active. Env defaults:
`AWS_REGION=us-east-1`, `PROJECT_NAME=agentcore-workshop`, `ENVIRONMENT=dev`.

**On macOS, prove the certs work before you trust any failure here.** These scripts
fetch the Cognito token with bare `urllib`, not boto3, so on a python.org build of
`python3.13` they all die with `CERTIFICATE_VERIFY_FAILED` while every `aws` command
keeps working — a failure that looks like a broken gateway and is not:

```bash
python3.13 -c "import ssl; print(ssl.get_default_verify_paths().cafile)"   # None == broken
"/Applications/Python 3.13/Install Certificates.command"                  # the fix
```

See `troubleshooting.md` → `CERTIFICATE_VERIFY_FAILED`.

---

## The whole set

```bash
./scripts/deploy.sh verify                                # every check this config promises; non-zero on failure
.venv/bin/python scripts/invoke.py "Reply with exactly: WORKSHOP OK"
.venv/bin/python scripts/invoke.py --tools                # tools registered on the GATEWAY
.venv/bin/python scripts/invoke.py --agui "…"             # agui-* patterns (typed SSE)
.venv/bin/python scripts/invoke.py --a2a code-agent "…"   # A2A sub-agent (JSON-RPC + SigV4)
.venv/bin/python scripts/test_gateway.py                  # gateway direct: tools/list + tools/call
.venv/bin/python scripts/test_memory.py                   # Memory data-plane operations
.venv/bin/python scripts/test_agent.py                    # interactive chat with a runtime
.venv/bin/python scripts/check_observability.py           # logs + traces actually accepted
.venv/bin/python scripts/check_observability.py --spans    # …and spans searchable end to end
.venv/bin/python scripts/check_network.py                 # runtimes really are in the VPC
```

---

## `invoke.py` — the main tool

```
.venv/bin/python scripts/invoke.py [PROMPT] [--session ID] [--tools] [--agui] [--a2a [COMPONENT]]
```

| Flag | Does |
|---|---|
| *(positional)* | the prompt |
| `--session ID` | reuse a session — see the length caveat below |
| `--tools` | list the gateway's MCP tools instead of invoking the agent |
| `--agui` | use the AG-UI protocol — required for `agui-*` patterns |
| `--a2a [COMPONENT]` | invoke an A2A sub-agent over JSON-RPC; defaults to `code-agent` |

Prefer `invoke.py` over hand-rolled calls: it picks the right auth mechanism per
target — Bearer JWT for the orchestrator, SigV4 for A2A sub-agents — which is
exactly the thing people get wrong.

**Session ids: the ≥33-character rule applies on one path and not the other, and
`invoke.py` enforces it on neither.** `new_session_id()` builds a 40-char id and the
`--session` help says `>=33 chars`, but the value is passed straight through
unvalidated. Both behaviours are measured:

| Path | 32-char id | 33-char id |
|---|---|---|
| orchestrator (HTTP, `--session`) | **accepted** — a 15-char id worked | fine |
| A2A (`--a2a`, boto3 `InvokeAgentRuntime`) | **rc=1**, unhandled traceback | fine |

So a session id that works all week on the orchestrator breaks the moment someone
tries it against a sub-agent. The boundary is exact — 33 passes, 32 does not — and
the last line of the traceback says so plainly:

```
botocore.exceptions.ParamValidationError: Parameter validation failed:
Invalid length for parameter runtimeSessionId, value: 32, valid min length: 33
```

It is client-side botocore validation, so no AWS call is made. The message is good;
what makes it look like a crash is that `invoke.py` does not catch it, so a room sees
twenty lines of stack trace. **Read the last line, not the traceback.** Just use ids
≥33 characters everywhere: `session-$(uuidgen | tr -d -)`.

**And `--session` does not give the default agent a memory.** Reusing a session id
does not make the `orchestrator` pattern recall anything — it never reads Memory. See
`agent-patterns.md`; a recall demo needs `strands-agent` or `langgraph-agent`.

### `--tools` answers a narrower question than it looks like

`--tools` and `test_gateway.py` talk to the gateway with a machine token. They
tell you what is **registered on the gateway**. That is not the same question as
what a given agent **loaded**.

```bash
.venv/bin/python scripts/invoke.py --tools
# sample-tool___text_analysis_tool, web-search___WebSearch
```

To see the agent's own view, ask a tool-using pattern:

```bash
.venv/bin/python scripts/invoke.py "List the names of the tools you have available. Names only."
```

The default `orchestrator` pattern has **no tools**, so it correctly answers
nothing. Use `strands-agent`, `langgraph-agent`, `claude-sdk-*` or `agui-*` for
that question.

---

## `verify.py` — the one check for the whole deployment

```bash
./scripts/deploy.sh verify                  # or: .venv/bin/python scripts/verify.py
```

The broadest single "is this platform actually working" probe, and the first thing
to collect when asking anyone for help. It does not add a check of its own — it
asks the deployment contract what *this* configuration promises and runs the tool
that proves each promise (`scripts/verify.py:56-81`):

| Stack in the footprint | Check it runs |
|---|---|
| `-gateway` | `test_gateway.py` — `tools/list` then a real `tools/call` |
| `-memory` | `test_memory.py` |
| `-observability` | `check_observability.py` |
| `-networking` | `check_network.py` |
| `-runtime-orchestrator` | `invoke.py` (adds `--agui` for `agui-*` patterns) |
| `-runtime-code-agent` | `invoke.py --a2a code-agent` |
| `-runtime-research-agent` | `invoke.py --a2a research-agent` |

**Any failed check exits 1** (`scripts/verify.py:110-113`). That is the point of
it, and it is worth saying explicitly because its predecessor did the opposite.

Two things to read carefully before treating a pass as proof:

- **It verifies what the configuration promises, not what the account contains.**
  The footprint comes from `expected_stacks()` against `platform.yaml` — schema
  defaults if that file is absent, env vars winning over both
  (`scripts/verify.py:44-52`). So a manifest with networking off will not run
  `check_network.py` even when a VPC is deployed and the runtimes are outside it.
  `OK: all 4 checks passed` means four claims held, and the header line prints the
  footprint it derived — read that line, not just the verdict.
- **Four stacks in the footprint have no check at all**: `-auth`, `-identity`,
  `-security`, and any `uc-*` use-case stack. `verify` is silent on identity and
  on the security stack, so it is not the answer to "are the controls working."
  It also runs `check_observability.py` without `--spans`, so span searchability
  is not covered.

In a federated deployment the footprint is role-dependent, so `verify.py` calls
`sts:GetCallerIdentity` and lets the account decide which half it is checking
(`scripts/verify.py:85-90`). Running it against the wrong account does not fail —
it checks a different, smaller thing.

**If you are looking at an older checkout, this command did not exist and its
predecessor could not fail.** `scripts/test.py` was the README's headline health
check; it swallowed invoke exceptions and printed them as expected, hardcoded the
default project name, marked zero discovered SSM parameters as a success, and had
no non-zero exit path anywhere — a broken deployment ended with `Done.` and exit
0. Its most alarming symptom was a red `✗ AccessDeniedException … Authorization
method mismatch` on step 2 of a perfectly healthy platform, because boto3's
`InvokeAgentRuntime` cannot authenticate against a `CUSTOM_JWT` runtime at all.
If someone reports that output, they are on a pre-`verify.py` checkout: the answer
is `git pull` and `deploy.sh verify`, not a configuration change.

---

## `test_gateway.py` — the gateway directly

```
.venv/bin/python scripts/test_gateway.py [--project NAME] [--env ENV]
```

Fetches an M2M token, runs `tools/list`, then a real `tools/call`. This is
module 5's and module 7's verify, and it is the fastest way to tell "the gateway
is broken" from "the agent cannot reach the gateway."

---

## `test_memory.py` — Memory operations

```
.venv/bin/python scripts/test_memory.py [--memory-arn ARN] [--project NAME] [--env ENV]
```

`--memory-arn` overrides the SSM lookup.

**What it does not prove:** it uses **your local credentials**, so it passes
regardless of whether the *runtime role* can reach Memory. Framework memory
integrations call data-plane actions — LangGraph's checkpointer lists events to
rehydrate a thread — so a runtime role missing `ListEvents` / `CreateEvent` /
`RetrieveMemoryRecords` fails at invoke while this script and the stack both look
fine. To check the role:

```bash
aws iam list-role-policies --role-name <project>-orchestrator-role
```

---

## `test_agent.py` — interactive chat

```
.venv/bin/python scripts/test_agent.py [--project NAME] [--env ENV] [--component COMPONENT]
```

`--component` defaults to `orchestrator`. Useful in a session when you want to
hand someone a prompt loop rather than re-running `invoke.py`.

---

## `check_observability.py` — the tracing claim

```
.venv/bin/python scripts/check_observability.py [--spans]
```

Three checks against live state:

1. the trace segment destination is `CloudWatchLogs` and `ACTIVE`
2. a CloudWatch Logs resource policy lets X-Ray write the span log groups
3. vended log delivery exists for each monitored AgentCore resource

`--spans` adds the end-to-end proof: it finds a span emitted in the last hour in
`aws/spans` and queries it back by `traceId` through Logs Insights — the same
path the Transaction Search console uses. It needs **at least one agent
invocation in the last hour** and is opt-in precisely because span delivery lags
invocation by a minute or two, which would make module 9's gate flaky.

```bash
.venv/bin/python scripts/invoke.py "hi"
sleep 120
.venv/bin/python scripts/check_observability.py --spans
# PASS: trace <id> searchable (5 spans, service agentcore_workshop_dev_orchestrator.DEFAULT)
```

**Do not "fix" this:** `aws xray batch-get-traces` and `get-trace-summaries`
return nothing for most traces. With Transaction Search, *all* spans are
searchable in `aws/spans`, while the classic X-Ray APIs only serve the indexed
sample (default rule: 1%). An empty trace-API result is expected, not a delivery
failure.

```bash
aws xray get-trace-segment-destination     # expect CloudWatchLogs / ACTIVE
```

**`PENDING` there is the one failure of this script that means "wait", not "fix".**
On a first-ever enablement in an account the destination is still `PENDING` when the
stack completes, so check 1 fails:

```
FAIL: trace segment destination is CloudWatchLogs but status is PENDING
```

Measured: **8m22s** from `CREATE_COMPLETE` to `ACTIVE`. Poll the command above and
re-run; all four checks then pass with nothing redeployed:

```
PASS: trace segment destination is CloudWatchLogs (ACTIVE)
PASS: X-Ray span delivery policy present (…-transaction-search-xray)
PASS: 3 vended log deliveries active
OK: logs and traces are being accepted
```

---

## `check_network.py` — the isolation claim

```
.venv/bin/python scripts/check_network.py [--expect-public]
```

Verifies runtime network placement. Two uses:

- After module C, plain `check_network.py` proves runtimes are actually in the
  VPC's private subnets — not merely that a VPC exists.
- `--expect-public` asserts the opposite, which is how you confirm a non-VPC
  deployment is public **deliberately** rather than accidentally.

**It checks two independent things and stops at the first failure**, so on a fresh
account you will meet them one at a time:

1. **AZ ids.** AgentCore supports a limited set per Region and AZ name → id mapping
   differs per account, so `max_azs=2` can land you outside the supported set with
   the stack still `CREATE_COMPLETE`. Fixing this needs a source edit *and* a stack
   destroy — see `troubleshooting.md` before you start.
2. **Runtime placement.** Deploying module C does not move existing runtimes into
   the VPC; they stay `PUBLIC` until redeployed.

Because of (2), **`--expect-public` passing is not evidence of anything on its own.**
Measured: with the networking stack `CREATE_COMPLETE` and all three runtimes still
public, `--expect-public` reported `OK: network placement matches the deployment's
claim` and exited 0. It is answering the question you asked, not the one you meant.
Once the runtimes moved it correctly returned exit 1 with
`FAIL: code-agent: expected networkMode PUBLIC, got VPC`.

A full pass looks like this:

```
PASS: private subnets are in supported AZs (use1-az2, use1-az1)
PASS: code-agent is in the VPC (2 subnets)
PASS: orchestrator is in the VPC (2 subnets)
PASS: research-agent is in the VPC (2 subnets)
OK: network placement matches the deployment's claim
```

Corroborate with the ENIs themselves — this is the artifact a compliance reviewer
asks for:

```bash
aws ec2 describe-network-interfaces \
  --filters Name=interface-type,Values=agentic_ai \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,AvailabilityZone,SubnetId,Status]' --output text
```

Expect **fewer ENIs than runtimes** — three runtimes in the VPC produced two
`agentic_ai` ENIs, one per private subnet, shared across runtimes. That is normal;
"one ENI per agent" is not the shape.

Finish by proving isolation did not break the thing you isolated:

```bash
.venv/bin/python scripts/invoke.py "Reply with exactly: VPC OK"
.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A VPC OK"
```

---

## Confirming what a deploy actually produced

Stack status alone is weak. These two answer "is the thing I asked for running?"

```bash
# Protocol + version + image the runtime is really on
ARN=$(aws ssm get-parameter --name /$PROJECT_NAME/$ENVIRONMENT/runtimes/orchestrator/arn \
  --query Parameter.Value --output text)
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${ARN##*/}" \
  --query '[agentRuntimeVersion,protocolConfiguration,agentRuntimeArtifact]'
# agui-* patterns → AGUI; A2A sub-agents → A2A; everything else → HTTP

# Did CodeBuild actually rebuild? Identical tags across two deploys means it did not.
aws ecr describe-images --repository-name $PREFIX-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-5:].{tags:imageTags,pushed:imagePushedAt}'
```

---

## The local dashboard

Status only, runs on your machine, no AWS resources:

```bash
.venv/bin/python dashboard/monitor.py &
python3 -m http.server 8888 -d dashboard/public
# http://localhost:8888
```

Good for a facilitated session — it gives the room something to watch during
module 6's silent seven minutes.

---

## Local checks that need no AWS account

Useful before a session, or in CI:

```bash
make lint                  # ruff
make validate-controls     # control-library ↔ catalog.yaml consistency
make test-controls         # pytest tests/ -q
make check-shell           # shellcheck + deploy-config + workshop-flow checks
.venv/bin/python -m infra_utils.platform_config platform.yaml
./scripts/deploy.sh workshop --dry-run --profile <p>
```

`make check-shell` uses `BASH ?= /opt/homebrew/bin/bash` — override `BASH` if
your bash 4+ lives elsewhere.

`cdk synth` is the cheap way to prove a feature flag does what it claims without
deploying. `docs/TESTING.md` in the repo has ready-made synth assertions per
control — flag off → resource count 0, flag on → resource present — which is a
good answer to "prove this control is real" in a security conversation.

---

## When a verify fails

**A failing verify is useful information, not a dead end.** The guided run offers
to continue; if you say yes, write down which module failed. Later modules build
on it, and the failure usually explains a stranger symptom two modules later.

Read the container logs before changing anything — every runtime failure so far
named its own cause there:

```bash
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT" \
  --start-time $(( ($(date +%s) - 900) * 1000 )) \
  --query 'events[].message' --output text | grep -iE 'error|denied|traceback'
```

Then go to `troubleshooting.md`.

## Collect these before asking anyone for help

```bash
./scripts/deploy.sh config
aws sts get-caller-identity
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'$PREFIX')].{n:StackName,s:StackStatus}" --output table
./scripts/deploy.sh verify
```
