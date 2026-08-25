# Troubleshooting

Symptom → cause → fix. Every entry here is a failure that actually happened
during development or a live workshop run, not a hypothetical.

**Two rules that save the most time:**

1. **Read the container logs before changing anything.** Every runtime failure
   so far named its own cause there:
   ```bash
   aws logs filter-log-events \
     --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT" \
     --start-time $(( ($(date +%s) - 900) * 1000 )) \
     --query 'events[].message' --output text | grep -iE 'error|denied|traceback'
   ```
   Get `<runtime-id>` from the ARN's last segment:
   `aws ssm get-parameter --name /<project>/<env>/runtimes/orchestrator/arn --query Parameter.Value --output text`

2. **A stack reaching `CREATE_COMPLETE` proves nothing about behaviour.** The
   verify scripts exist because stacks have completed successfully while the
   thing they promise was broken. Run them.

---

## Quick index

| Symptom | Jump to |
|---|---|
| `declare: -A: invalid option` | [Bash 3.2](#the-script-dies-on-declare--a-invalid-option) |
| Script freezes with no output | [npx CDK hang](#the-script-freezes-after-the-docker-check) |
| `python3.13: NOT FOUND` | [Prerequisites](#python313-not-found) |
| `AWS credentials invalid or expired` | [Credentials](#aws-credentials-invalid-or-expired) |
| Deploy fails, no useful error | [CloudFormation events](#a-stack-fails-and-the-cdk-output-is-not-enough) |
| Agent invoke returns `Unauthorized` | [JWT verification](#invoke-returns-unauthorized) |
| Agent invoke returns HTTP 424 | [Protocol mismatch](#invoke-returns-http-424) |
| `Authorization method mismatch` | [SigV4 vs JWT](#authorization-method-mismatch-on-invoke) |
| Agent has no gateway tools | [Gateway tools](#the-agent-reports-no-tools-or-only-the-code-interpreter) |
| Traces never appear in X-Ray | [Tracing](#no-traces-appear-anywhere) |
| `batch-get-traces` returns nothing | [1% sampling](#aws-xray-batch-get-traces-returns-nothing-for-my-trace) |
| Destroy fails on an export | [Teardown order](#destroy---stack-fails-with-export--cannot-be-deleted) |
| Networking destroy fails on ENIs | [ENI drain](#destroying-the-networking-stack-fails-on-subnets-or-security-groups) |
| `enable_resource_policies` needs ORG_ID | [ORG_ID](#enable_resource_policiestrue-requires-an-aws-organizations-id) |
| Model access denied / model not found | [Bedrock models](#bedrock-says-the-model-is-not-available-or-access-is-denied) |
| Redeploy changed nothing | [Image tags](#i-changed-agent-code-and-the-redeploy-changed-nothing) |
| `platform.yaml is invalid` | [Config validation](#platformyaml-is-invalid) |
| Stale answers keep coming back | [workshop.env](#old-answers-keep-coming-back) |

---

## Local setup

### The script dies on `declare: -A: invalid option`

macOS ships bash 3.2 (2007) as `/bin/bash`; the script needs associative
arrays. It checks for this and prints the remedy, but if you invoked it in a
way that bypassed the check:

```bash
brew install bash
bash scripts/deploy.sh deploy          # explicit new bash
# or: hash -r   (so PATH picks up /opt/homebrew/bin/bash)
```

### The script freezes after the docker check

Older copies of this repo probed the CDK CLI with a bare `npx cdk --version`.
When `aws-cdk` is not in the npx cache, npx asks "Ok to proceed?" and waits —
with output suppressed and no TTY, forever. The fix is already in
`check_prereqs` (`npx --no-install cdk --version`), so if you see this you are
on an old checkout. Meanwhile:

```bash
npm install -g aws-cdk
```

### python3.13 NOT FOUND

The scripts require exactly `python3.13` on PATH (not `python3`). Install it,
then rebuild the venv:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Do I need Docker?

**No.** Container images are built remotely in AWS CodeBuild. `check_prereqs`
reports docker as optional and continues without it. You only need a container
runtime if you want to build and run an agent image locally while developing.

---

## Credentials and permissions

### `AWS credentials invalid or expired`

Refresh them and retry. Long deploys can outlive a session, so this can also
appear *mid-run*:

```bash
aws sso login          # or your credential process / Isengard refresh
aws sts get-caller-identity   # confirm before retrying
```

If a deploy died mid-stack, re-run the same command — CDK picks up from the
current stack state. A stack stuck in `UPDATE_ROLLBACK_COMPLETE` is safe to
deploy onto again.

### Environment variables silently override your profile

`AWS_ACCESS_KEY_ID` / `AWS_SESSION_TOKEN` in the environment beat
`AWS_PROFILE`. If commands hit the wrong account, that is usually why:

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sts get-caller-identity --query Account --output text   # verify the account
```

### CDK bootstrap failed

The account/region needs the `CDKToolkit` stack. The script bootstraps
automatically; failure usually means the wrong account or missing IAM
permission to create that stack. Confirm the account first
(`aws sts get-caller-identity`), then retry.

---

## Deploy failures

### A stack fails and the CDK output is not enough

Ask CloudFormation directly — the resource-level reason is what you want:

```bash
aws cloudformation describe-stack-events --stack-name <stack> \
  --query "StackEvents[?contains(ResourceStatus,'FAILED')].{r:LogicalResourceId,reason:ResourceStatusReason}" \
  --output json | head -30
```

### `enable_resource_policies=true` requires an AWS Organizations ID

The memory resource policy renders `aws:PrincipalOrgID` and cannot be built
without it:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

If you are not in an Organization, leave `enable_resource_policies` off.

### Bedrock says the model is not available, or access is denied

Two independent causes:

- **Model access not enabled** in this account/region — enable it in the
  Bedrock console (Model access), then retry.
- **The model ID has aged out.** Dated model IDs get marked Legacy and are
  rejected in fresh accounts. Every pattern takes an override rather than
  hardcoding, so point it at a current cross-region inference profile:
  ```bash
  export MODEL_ID=us.anthropic.claude-sonnet-4-6     # or your current profile
  ./scripts/deploy.sh deploy --stack <prefix>-runtime-orchestrator
  ```

### VPC mode fails on availability zones

AgentCore supports a limited set of AZ **ids** per region, and AZ *name* →
*id* mapping differs per account (`us-east-1a` is not the same zone in two
accounts). Module C's verify catches it:

```bash
python scripts/check_network.py
```

### I changed agent code and the redeploy changed nothing

Image tags are a content hash of the source plus the selected pattern, and
CodeBuild only reruns when the hash changes. Identical tags across two deploys
means no rebuild happened:

```bash
aws ecr describe-images --repository-name <prefix>-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-5:].{tags:imageTags,pushed:imagePushedAt}'
```

If you expected a rebuild, confirm you edited a file inside the build context
(`agent-code/`) and not something excluded from it.

### `platform.yaml is invalid`

Validation is deliberately strict and reports **every** problem at once,
before any AWS call. Unknown keys are errors, not no-ops (a typo would
otherwise silently do nothing):

```bash
python -m infra_utils.platform_config platform.yaml
```

### Old answers keep coming back

The wizard remembers answers in `workshop.env`, and `platform.yaml` overrides
it. To see what is actually in effect, and to start clean:

```bash
./scripts/deploy.sh config           # effective values, with their source
./scripts/deploy.sh config --reset   # delete saved answers
```

---

## The agent does not work

### Invoke returns `Unauthorized`

Applies to the patterns that verify identity themselves — `strands-agent`,
`langgraph-agent`, the `claude-sdk-*` and `agui-*` patterns (via
`agent-code/shared/auth.py`). The default `orchestrator` pattern reads no token
at all, so it never produces this.

Those agents check the caller's JWT (signature against the issuer's JWKS, plus
expiry, issuer and client) rather than trusting that the runtime authorizer ran
— see [`IDENTITY.md`](IDENTITY.md). The response is deliberately generic;
**the reason is in the container logs**. Common causes:

- `COGNITO_ISSUER_URL` / `COGNITO_ALLOWED_CLIENTS` not injected into the
  runtime → the agent refuses rather than decoding unverified.
- A token from a different user pool, or an expired one.
- Calling with a machine (M2M) token where the client id is not in the allowed
  clients list.

### Invoke returns HTTP 424

The container is not serving the protocol the runtime expects. The usual cause
is invoking an **A2A sub-agent** (`code-agent`, `research-agent`) with the HTTP
payload shape: they speak JSON-RPC 2.0 on port 9000, so `{"prompt": ...}` gets
you a 424. Use the A2A path, which sends a `message/send` envelope:

```bash
python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"
python scripts/invoke.py --a2a research-agent "…"
```

If a sub-agent you wrote yourself 424s, check it serves the contract: `/` POST
(JSON-RPC), `/.well-known/agent-card.json` GET, and `/ping` GET returning
`{"status": "Healthy"}`, all on **0.0.0.0:9000**. `agent-code/shared/a2a_serve.py`
does this for the shipped sub-agents.

Otherwise check the runtime's protocol against what its code serves:

```bash
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> \
  --query '[agentRuntimeVersion,protocolConfiguration]'
```

Expected values (`infra_utils/runtime_protocol.py`): the A2A sub-agent runtimes
report `A2A`, an MCP-server runtime `MCP`, and the orchestrator runtime `HTTP`
— except for the `agui-*` patterns, which report `AGUI` because they serve
AG-UI's typed SSE events on the same endpoint.

### `Authorization method mismatch` on invoke

The runtime's inbound auth and your request disagree, and it cuts **both** ways:

| Runtime | Auth it expects | How to call it |
|---|---|---|
| orchestrator (`HTTP`/`AGUI`/`MCP`) | Bearer JWT — it has a CUSTOM_JWT authorizer | `invoke.py` / `invoke.py --agui` |
| A2A sub-agents (`A2A`) | **SigV4** — no authorizer; guarded by IAM `InvokeAgentRuntime` | `invoke.py --a2a <component>` |

A2A is not a client-facing protocol, so those runtimes deliberately get no JWT
authorizer (`infra_utils/runtime_protocol.py`) — sending a bearer token to one
is rejected exactly like sending SigV4 to the orchestrator. Prefer
`scripts/invoke.py`, which picks the right mechanism per target.

### The agent reports no tools, or only the code interpreter

First: is this the `orchestrator` pattern? It ships with no tools by design.
Tools are consumed by `strands-agent`, `langgraph-agent`, the `claude-sdk-*`
and `agui-*` patterns.

Otherwise, gateway tools load through AgentCore Identity: the agent exchanges
M2M credentials for a gateway token via the token vault. Two failure shapes,
and telling them apart saves time:

- **Silently no tools** — the agent answers, just without them. Only two
  conditions do this (both `return None` in `tools/gateway.py`):
  `GATEWAY_CREDENTIAL_PROVIDER_NAME` unset, or the gateway URL unresolvable.
  Check both env vars on the runtime, and look for `[GATEWAY]` warnings in the
  container logs — the client logs why it gave up.
- **The invoke fails outright** — if the runtime role cannot read the vault's
  secret (`secretsmanager:GetSecretValue` on
  `bedrock-agentcore-identity!default/oauth2/*`), the token fetch raises
  AccessDenied inside the MCP client and the agent aborts rather than degrading.
  An invoke that errors instead of answering points here.

Then confirm the gateway side is healthy and the tool is actually registered:

```bash
python scripts/test_gateway.py       # tools/list + one tools/call
```

Also note the web-search built-in connector only exists in some regions; it is
region-gated in `app.py`, so in an unsupported region the tool is absent by
design.

### Memory-backed patterns fail at invoke

Framework memory integrations call data-plane actions (for example LangGraph's
checkpointer lists events to rehydrate a thread). If the **runtime role** lacks
`ListEvents`/`CreateEvent`/`RetrieveMemoryRecords`, the pattern fails at invoke
while the stack looks fine.

Note what does *not* prove this either way: `scripts/test_memory.py` uses
**your local credentials**, so it passes regardless of the runtime role's
permissions. It confirms the memory resource works; it says nothing about the
agent's access. To check the role itself, invoke a memory-backed pattern and
read the container log, or inspect the role's policy directly:

```bash
aws iam list-role-policies --role-name <project>-orchestrator-role
```

---

## Observability

### No traces appear anywhere

Runtimes emit OTLP spans even when the account cannot receive them: while the
X-Ray trace segment destination is still `XRay`, every batch is rejected with
HTTP 400 and the deployment still reports success. The observability stack sets
this, so the usual cause is deploying with `enable_transaction_search=false`:

```bash
python scripts/check_observability.py          # destination + policy + deliveries
aws xray get-trace-segment-destination         # expect CloudWatchLogs / ACTIVE
```

Full explanation in [`TRACING.md`](TRACING.md).

### `aws xray batch-get-traces` returns nothing for my trace

Expected, not a failure. With Transaction Search, **all** spans are searchable
in the `aws/spans` log group, while the classic X-Ray APIs only serve the
indexed sample (default rule: 1%). Search spans the way the console does:

```bash
python scripts/check_observability.py --spans   # needs an invoke in the last hour
```

Also give it time — span delivery lags an invocation by a minute or two, which
is exactly why `--spans` is opt-in and not part of module 9's verify.

---

## Teardown

### `destroy --stack` fails with "Export … cannot be deleted"

Working as intended. A targeted destroy passes `--exclusively`, so
CloudFormation refuses rather than silently cascading into stacks that depend
on the target — which would take the platform out from under other teams.
The error names the consumer. Either destroy the dependents first, or take the
whole environment down:

```bash
./scripts/deploy.sh destroy            # --all, cascade is intended here
```

### Destroying the networking stack fails on subnets or security groups

AgentCore leaves `agentic_ai` ENIs behind for up to ~8 hours after runtimes
stop using a VPC. Check, then retry later:

```bash
aws ec2 describe-network-interfaces \
  --filters Name=interface-type,Values=agentic_ai \
  --query 'NetworkInterfaces[].{id:NetworkInterfaceId,status:Status,subnet:SubnetId}'
```

NAT gateways and endpoints *do* delete, so the leftovers are not costing you
anything meaningful while you wait.

### Deleting the observability stack does not disable Transaction Search

Deliberate. The trace segment destination is account- and region-scoped, and
other workloads may depend on it by the time you tear this down. Revert it
yourself if you really want to:

```bash
aws xray update-trace-segment-destination --destination XRay
```

---

## Still stuck

Collect these before asking for help — they answer the first three questions
anyone will have:

```bash
./scripts/deploy.sh config                        # effective configuration
aws sts get-caller-identity                       # account and identity
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'<prefix>')].{n:StackName,s:StackStatus}" --output table
./scripts/deploy.sh verify                        # platform health (non-zero on any failed claim)
```
