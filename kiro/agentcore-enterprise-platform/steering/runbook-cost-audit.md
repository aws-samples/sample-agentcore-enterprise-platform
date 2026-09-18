# What is this costing right now

Read this when someone asks what this is costing, whether anything was left
running, whether the account is clean, what is still deployed from a previous
session, or why a bill went up after a workshop.

**This is a runbook, not reference material.** Follow it in order. Anything that
creates, changes, deletes or bills goes one command at a time: state what it
does and what it costs, then stop and wait for approval. Never group one of
those with anything else. The halt conditions at the end are not advisory.

Every command here is **read-only**. Nothing in this runbook changes, creates or
deletes anything, so approvals are cheap — ask for them freely rather than
guessing at an answer. That is the whole point: the alternative to running these
is someone estimating from memory, and the standing costs here are the ones people
estimate wrong.

Answer three questions in order, because they have very different urgency:

1. **What is billing per hour?** This is what makes "we left it up over the
   weekend" expensive. It is also the shortest list.
2. **What is billing per month?** Small, and the reason a forgotten account
   costs real money over a quarter.
3. **What survived a teardown?** Not everything the accelerator creates is owned
   by a stack.

If the answer to any of them is "more than they expected", hand off to
`teardown-platform` rather than deleting things here.

## Step 0 — which account, and what is deployed

```bash
aws sts get-caller-identity --query Account --output text
```

Say the account id back before reporting numbers. A cost report against the wrong
account is worse than no report.

```bash
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'agentcore-workshop-dev')].[StackName,StackStatus,CreationTime]" \
  --output table
```

`$PREFIX` defaults to `agentcore-workshop-dev` (`${PROJECT_NAME}-${ENVIRONMENT}`).
Substitute if either was overridden — and if you are auditing an account that hosted
a multi-team session, there may be several prefixes (`team-a-dev`, `team-b-dev`).
Drop the filter entirely if you are not sure what to look for:

```bash
aws cloudformation describe-stacks \
  --query 'Stacks[?StackStatus!=`DELETE_COMPLETE`].[StackName,StackStatus]' --output table
```

`CreationTime` is the useful column. It turns "is this costing us anything" into
"this has been up for eleven days", which is the number that actually gets a
decision made.

## Step 1 — the hourly meters

Only `enable_networking=true` creates any of these, which means `greenfield`,
`migration` and `multi-agent` leave **nothing** billing hourly. If no
`-networking` stack appeared in Step 0, say so plainly and skip to Step 2 —
that is a genuinely good answer, not an incomplete one.

```bash
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].[NatGatewayId,VpcId,CreateTime]' --output table
```

One NAT gateway is expected per deployed VPC (`nat_gateways=1`). It bills per hour
plus per GB processed, and it is the single largest idle line item in the whole
platform.

```bash
aws ec2 describe-vpc-endpoints \
  --query "VpcEndpoints[?VpcEndpointType=='Interface'].[VpcEndpointId,ServiceName,length(SubnetIds)]" \
  --output table
```

Expect **5** interface endpoints, each in **2** subnets, so **10 endpoint-AZ-hours**
— `bedrock-runtime`, `ecr.api`, `ecr.dkr`, `logs`, `bedrock-agentcore.gateway`. The
third column is the multiplier and is the part people miss. A sixth S3 endpoint of
type `Gateway` is free and will not appear in this filter.

**There is no flag to reduce either number.** `enable_vpc_endpoints=True` is a
literal at the call site (`app.py:197`) and `max_azs=2` is hardcoded. If the answer
someone wants is "make the networking demo cheaper", the honest reply is: turn
networking off, or edit the stack. Do not go looking for a `-c` flag.

## Step 2 — the monthly trickle

Small individually. Worth listing because none of them appear in a stack list by a
name anyone recognises.

```bash
aws kms list-aliases \
  --query "Aliases[?starts_with(AliasName,'alias/agentcore-workshop-dev')].[AliasName,TargetKeyId]" \
  --output table
```

Module E creates one customer-managed key with rotation on, aliased
`alias/$PREFIX-agentcore`. Per key per month.

```bash
aws secretsmanager list-secrets --query 'SecretList[].[Name,CreatedDate]' --output table
```

Deliberately unfiltered: these are created from IdP or API-key prompts, so the
**names are whatever the operator typed** and no prefix filter is reliable. This is
the line item people forget, precisely because nothing in the stack list names it.

```bash
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/agentcore-workshop-dev \
  --query 'logGroups[].[logGroupName,storedBytes,retentionInDays]' --output table
```

A `retentionInDays` of `None` means never expires, and none of the accelerator's
groups set retention, so they accumulate for as long as the account does.

**Keep this prefix-scoped.** The tempting version —
`logGroups[?storedBytes>\`0\`]` with no prefix — returned **187 groups** in a
lightly-used test account, almost all of them unrelated. An audit that buries its
finding in 187 rows has not answered the question. Step 3 has the full set of
prefixes worth sweeping.

Not worth auditing, and say so rather than running commands to prove it: **ECR
image storage** is capped at the last 10 images by a lifecycle rule and the
repository is `RemovalPolicy.DESTROY` with `empty_on_delete=True`
(`stacks/runtime_stack.py:91-109`), so it goes away with the stack. **AgentCore
Runtime, Gateway, Code Interpreter and Cognito** are per-invocation or
per-request; idle they cost nothing, however alarming the stack list looks.

## Step 3 — what survived, or is about to

Two categories outlive the stacks that made them.

```bash
aws logs describe-log-groups \
  --query "logGroups[?starts_with(logGroupName,'/aws/bedrock-agentcore/') || starts_with(logGroupName,'/aws/lambda/agentcore-workshop-dev') || starts_with(logGroupName,'/aws/codebuild/agentcore-workshop-dev')].[logGroupName,storedBytes]" \
  --output table
```

Log groups are created by the *service*, not the stack, so a clean delete leaves
them. A measured teardown left 13 — 7 Lambda, 3 CodeBuild, 3 AgentCore runtime.
Note that the runtime groups use **underscores**
(`/aws/bedrock-agentcore/runtimes/agentcore_workshop_dev_orchestrator`), so a
hyphenated prefix sweep silently finds none of them. Also note
`/aws/bedrock-agentcore/` is a **shared namespace** — measured in a test account
with no accelerator deployed at all, this query still returned runtime log groups,
from unrelated AgentCore work by other tools. **Match the project prefix, not the
service prefix**, and never report a `/aws/bedrock-agentcore/` hit as an orphan of
this platform without checking the name actually carries the project prefix.

```bash
aws bedrock-agentcore-control list-agent-runtimes --query 'agentRuntimes[].agentRuntimeName' --output text
aws bedrock-agentcore-control list-gateways --query 'items[].name' --output text
aws bedrock-agentcore-control list-memories --query 'memories[].id' --output text
```

Control-plane resources that a failed teardown can strand. If a stack is gone but
its runtime still lists here, that is a real orphan and worth naming in the report.
Empty output from all three is the clean state.

## Step 4 — the account-wide one

```bash
aws xray get-trace-segment-destination
```

`Destination: CloudWatchLogs` with `Status: ACTIVE` means **Transaction Search is
on**, which changes span-ingestion pricing for the **entire account** — not just
this platform. It defaults on and **stays on after teardown by design**, because
other workloads may have come to depend on it.

This is the one item on this list that a teardown will not fix and that you should
not silently switch off, because it is shared. Report it as a standing account-level
decision and let its owner decide.

```bash
aws cloudtrail describe-trails --query 'trailList[].[Name,IsMultiRegionTrail]' --output text
```

Module E's trail (`$PREFIX-agentcore-trail`) is management-events-only and
single-region, so it is free — **unless the account already had a management-events
trail**, in which case this is the second copy and every event bills. Two rows here
is the finding.

## Step 5 — report it as a decision, not a dump

Give them, in this order:

1. **Per hour, right now** — the NAT count and the endpoint-AZ count, or the
   sentence "nothing in this account is billing hourly", which is often the true
   answer and the one they most want.
2. **How long it has been up**, from `CreationTime`. This is what makes the number
   mean something.
3. **Per month** — keys, secrets, retained logs.
4. **Orphans** — anything in Step 3 whose stack is gone.
5. **Account-wide** — Transaction Search state, and a second CloudTrail if present.

Then one recommendation, and be direct about it: keep it up, tear it down today
(`teardown-platform`), or tear down only networking to stop the hourly meter while
keeping the platform. Give meters and counts, never invented dollar amounts —
rates vary by Region and change, and a wrong number here gets repeated to a
customer.

## Halt conditions

- The account id is not the one they named. Stop and confirm before reporting.
- You are about to quote a dollar figure. You do not have current pricing; give
  the meters and let them price it, or point at Cost Explorer for actuals.
- An audit turns into a cleanup. Deleting things is `teardown-platform`, with its
  own confirmations — do not start deleting from inside a read-only audit because
  the answer looked bad.
