# Verify the platform

Read this when someone asks whether their platform is healthy, wants to confirm
a deploy really worked, or needs evidence for a review.

**This is a runbook, not reference material.** Follow it in order. Anything that
creates, changes, deletes or bills goes one command at a time: state what it
does and what it costs, then stop and wait for approval. Never group one of
those with anything else. The halt conditions at the end are not advisory.

`CREATE_COMPLETE` proves resources exist. It does not prove they work — stacks
have completed while the thing they promise was broken, which is why these
scripts exist. This runbook produces evidence, and is honest about the limits of
each piece of it.

## The rule

**Propose one command at a time and wait.** Almost everything here is read-only,
so these are cheap approvals — but `invoke.py` calls a model and bills per token,
so label it pay-per-use rather than free.

## Step 0 — what is actually deployed (read-only)

Do not verify from an assumption about which modules were run.

```bash
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'agentcore-workshop-dev')].[StackName,StackStatus]" \
  --output table
```

Build the checklist from what this returns. `$PREFIX` defaults to
`agentcore-workshop-dev` (`${PROJECT_NAME}-${ENVIRONMENT}`). The SSM paths below
are **not** `$PREFIX` — they are `/$PROJECT_NAME/$ENVIRONMENT/…`, slash-separated,
so substitute if either was overridden rather than pasting the literal.

## Step 1 — run the built-in verify first

```bash
./scripts/deploy.sh verify; echo "verify rc=$?"
```

It derives the footprint from this configuration and runs the matching check for
each promised stack — gateway, memory, observability, networking, and a live
invoke per runtime — exiting non-zero if any of them fails. Read the `Footprint:`
line it prints, not just the verdict: it tells you which claims were actually
tested.

That gets you most of the matrix below in one read-only pass. Go on to the matrix
anyway, for the two reasons it still exists:

- **`verify` has no check for `-auth`, `-identity`, `-security`, or a `uc-*`
  use-case stack**, and it omits `--spans`. Those rows are yours.
- **When `verify` fails, it tells you *which* check failed, not why.** The matrix
  is how you isolate it — and `troubleshooting.md` is how you fix it.

Do not report a platform as verified on `rc=0` alone. Say which footprint it
checked.

## Step 2 — the matrix

Run only the rows whose stack exists. Report each as PASS / FAIL / SKIPPED with
the reason, and never mark a row PASS because the stack was complete.

| Layer | Stack | Command | Cost |
|---|---|---|---|
| Identity | `-auth` | `aws ssm get-parameter --name /agentcore-workshop/dev/auth/issuer-url` | read-only |
| Credential provider | `-identity` | `aws ssm get-parameter --name /agentcore-workshop/dev/identity/gateway-credential-provider-name` | read-only |
| Gateway | `-gateway` | `.venv/bin/python scripts/test_gateway.py` | read-only |
| Tools visible to the agent | `-gateway` | `.venv/bin/python scripts/invoke.py --tools` | pay-per-use |
| Memory | `-memory` | `.venv/bin/python scripts/test_memory.py` | read-only |
| Agent | `-runtime-orchestrator` | `.venv/bin/python scripts/invoke.py "Reply with exactly: WORKSHOP OK"` | pay-per-use |
| A2A | `-runtime-code-agent` | `.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"` | pay-per-use |
| Observability | `-observability` | `.venv/bin/python scripts/check_observability.py` | read-only |
| Traces searchable | `-observability` | `.venv/bin/python scripts/check_observability.py --spans` | read-only |
| Network isolation | `-networking` | `.venv/bin/python scripts/check_network.py` | read-only |
| Security controls | `-security` | see "what a green security stack does not prove" | read-only |

## Step 3 — read the results honestly

Four of these mean less than their names suggest. If you report them without the
caveat, you have handed someone false assurance — which is worse than a failure
they can act on.

**`test_memory.py` is not an isolation proof.** It uses **your local
credentials**, so it passes regardless of what the runtime role is permitted to
do. It proves the memory resource works, not that the agent can reach it.

**`invoke.py --tools` answers a narrower question than it looks like.** It lists
what the *gateway* exposes. The default `orchestrator` agent consumes none of
them — it ships toolless. So a populated `--tools` list plus an agent that says
it has no tools is two correct answers, not a contradiction.

**A green `-security` stack proves existence, not enforcement.** The KMS CMK and
CloudTrail exist. Cedar ships in `LOG_ONLY` with a permit that is unconstrained on
principal and resource; the egress filter masks rather than blocks;
`IsMultiRegionTrail` is `false` and hardcoded. Enumerate what is actually
enforced from `steering/security.md` rather than reporting the stack status.

**`enable_networking=true` is not air-gapped.** Private subnets keep a NAT route,
by design. `check_network.py` proves the ENIs are in the private subnets, not that
egress is closed. Say which claim you are making.

## Step 4 — the two failures that are usually not failures

**`check_observability.py` fails the first time in a new account.** Enabling
Transaction Search is asynchronous and outlasts the stack that requested it. Check
the destination, wait, re-run:

```bash
aws xray get-trace-segment-destination
```

`--spans` is deliberately not part of module 9's verify because span delivery lags
behind the deploy. A fresh platform with no traffic has no spans to find; generate
one with `invoke.py` first, then wait before asserting.

**`check_network.py` reporting the agent is not in the VPC** is correct if the
runtimes were deployed before networking. A runtime keeps `networkMode: PUBLIC`
until it is redeployed. The fix is a redeploy, not a re-verify:

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module 6
```

`check_network.py` stops at the first failure, so fix and re-run rather than
reading one failure as the complete picture. `--expect-public` asserts the
opposite posture, for confirming a deliberately public deployment.

## Step 5 — report

Give the matrix, then three sentences: what is proven, what is unproven and why,
and what to do about any FAIL. If a row was skipped because its stack is not
deployed, say "not deployed" rather than leaving it blank — an empty cell reads
as a pass.

For a security review, `steering/security.md` has the control-by-control
inventory, including which controls are advisory in their shipped configuration.
That inventory is the deliverable, not this matrix.

## If something fails and you cannot place it

Route by **symptom** in `steering/troubleshooting.md` — it is indexed by what the
participant sees, not by which script emitted it. Collect the evidence listed at
the end of `steering/verify.md` before escalating: the command, its full output,
the stack status, and the Region and account.
