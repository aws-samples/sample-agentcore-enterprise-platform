# Tear down the platform

Read this when someone is finished with a deployment, a session is ending, or
they need to confirm an account is actually clean.

**This is a runbook, not reference material.** Follow it in order. Anything that
creates, changes, deletes or bills goes one command at a time: state what it
does and what it costs, then stop and wait for approval. Never group one of
those with anything else. The halt conditions at the end are not advisory.

A destroy that "finished" is not the same as an account that is clean. This
runbook is built from a measured teardown: the destroy exited non-zero after 11.6
minutes having deleted 4 of 10 stacks, and the NAT gateway was still `available`.
Check, do not assume.

## The rule

**Propose one command at a time and wait.** Deletions are irreversible, so state
what each one destroys before proposing it. Every check here is read-only — run
them freely.

## Step 1 — say what is about to be destroyed (read-only)

```bash
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'agentcore-workshop-dev')].[StackName,StackStatus]" \
  --output table
```

Confirm the account and Region out loud before deleting anything:

```bash
aws sts get-caller-identity && echo "${AWS_REGION:-<unset>}"
```

Ask explicitly whether anything in this platform is still needed. Also ask
whether anything **else** in the account depends on `CDKToolkit` — the answer is
usually yes, and it is not part of this teardown.

## Step 2 — destroy, and check the exit code

The exit code is the whole point of this step. `destroy` runs
`npx cdk destroy --all --force`, and **CDK stops at the first stack it cannot
delete.** Deletion order is reverse-dependency, which puts `-networking` **last** —
so an unrelated failure early in the order leaves the NAT gateway running.

```bash
./scripts/deploy.sh destroy; echo "destroy rc=$?"
```

~12 minutes for a full platform. Do not report success without reading that `rc`.

**A full destroy now sweeps behind itself, and you must know which prompts are
which.** After `--all`, `sweep_leftovers` (`scripts/deploy.sh:694-751`) asks
CloudFormation what still exists under the prefix — the stacks the *current*
config cannot see, which is the profile-switch case where a manifest with
networking off leaves a live NAT — and separately lists the secrets this script
creates outside CloudFormation (`-idp-client-secret`, the three 3LO OAuth
secrets, the three API keys). It only runs on an untargeted destroy, so
`destroy --stack <x>` never sweeps.

Three behaviours, and the middle one is the one that bites:

| Invocation | What the sweep does |
|---|---|
| interactive | asks per category: stacks, then secrets |
| `--yes` | deletes both, secrets with **no recovery window** |
| `NON_INTERACTIVE=1` without `--yes` | **reports and leaves** — "They may bill hourly" |

That last row is deliberate: CI must not delete resources the config does not
declare. It also means a scripted teardown can exit 0 having told you, in a
warning you did not read, that the NAT is still running. If you are tearing down
from CI, either pass `--yes` or treat the sweep's output as a task list.

The sweep covers stacks and secrets. It does **not** cover log groups or the
`agentic_ai` ENIs, which is why Steps 3 and 5 still exist.

## Step 3 — confirm the expensive things are actually gone

Do this whatever the exit code said. This is the check that catches an aborted
destroy:

```bash
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].NatGatewayId' --output text
```

Empty is the only acceptable answer. If it returns an id, the hourly meter is
still running regardless of what the destroy printed.

```bash
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'agentcore-workshop-dev')].[StackName,StackStatus]" \
  --output table
```

## Step 4 — the two failures that are expected

Neither is real breakage. Recognising them is the difference between a five-minute
finish and an afternoon of debugging.

### `DELETE_FAILED` with `NotStabilized` on an AgentCore runtime

The message is "Request timed out while deleting
`AWS::BedrockAgentCore::Runtime`". That is a **stabilization timeout, not a
failed deletion** — the runtime is already gone. Confirm it, then just retry:

```bash
aws bedrock-agentcore-control list-agent-runtimes --query 'agentRuntimes[].agentRuntimeName' --output text
```

Empty means the runtime is deleted and only CloudFormation's bookkeeping is
behind. Retry the stack delete — it took **34 seconds** in the measured case:

```bash
aws cloudformation delete-stack --stack-name <stack>
aws cloudformation wait stack-delete-complete --stack-name <stack>
```

Ignore the handler's advice to delete it from the AWS console. There is nothing
there to delete.

### The networking stack failing on subnets or security groups

AgentCore's `agentic_ai` ENIs outlive their runtimes and hold the subnets. **Two
different clocks, and conflating them causes the wrong decision:** the delete
fails **fast**, in about 17 minutes, and then you wait roughly **8 hours** before a
retry succeeds. It is not an 8-hour hang.

```bash
aws ec2 describe-network-interfaces \
  --filters Name=interface-type,Values=agentic_ai \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,Status]' --output table
```

These **cannot be force-deleted.** `RequesterManaged` is `false` and the
description is blank, so they look account-owned, but the attachment is
`ela-attach-…` with `InstanceOwnerId: amazon-aws`. Manual
`delete-network-interface` is refused. There is no force. Wait and retry the stack
delete later.

**The NAT and the endpoints delete before the failure**, so once the destroy has
actually reached `-networking`, the wait costs nothing meaningful. That is only
true if it got there — which is why Step 3 exists.

### If the destroy aborted before reaching networking

CDK's serial deletion means one stuck stack holds the rest hostage, including
`-auth` — the stack whose CloudFormation export carries the M2M client secret in
plaintext. Delete the blockers individually so the queue drains:

```bash
aws cloudformation delete-stack --stack-name agentcore-workshop-dev-gateway
aws cloudformation delete-stack --stack-name agentcore-workshop-dev-identity
aws cloudformation delete-stack --stack-name agentcore-workshop-dev-security
aws cloudformation delete-stack --stack-name agentcore-workshop-dev-auth
```

Then re-run `./scripts/deploy.sh destroy` for whatever is left.

## Step 5 — sweep the orphans

Service-created log groups are **not** owned by the stacks, so they survive a
clean delete. A real teardown left **13** across three classes: 7 Lambda, 3
CodeBuild, 3 AgentCore runtime.

Two traps make a naive sweep miss or over-reach:

- **Runtime log groups use underscores.** `$PREFIX` is `agentcore-workshop-dev`
  but the runtime group is `/aws/bedrock-agentcore/runtimes/agentcore_workshop_dev_orchestrator`.
  A sweep keyed on the hyphenated prefix finds none of them.
- **`/aws/bedrock-agentcore/` is a shared namespace.** Deleting the prefix
  wholesale takes out unrelated groups — 15 of them in the measured account. Match
  the project prefix, not the service prefix.

List first, delete second — and note that **zsh does not word-split unquoted
variables**, so a `for g in $LIST` loop passes the whole list as one name and
fails with a length error:

```bash
PREFIX=agentcore-workshop-dev
aws logs describe-log-groups --query "logGroups[?\
starts_with(logGroupName,'/aws/lambda/${PREFIX}')||\
starts_with(logGroupName,'/aws/codebuild/${PREFIX}')||\
starts_with(logGroupName,'/aws/bedrock-agentcore/runtimes/${PREFIX//-/_}')\
].logGroupName" --output text | tr '\t' '\n' > /tmp/orphans.txt

cat /tmp/orphans.txt          # review before deleting

while IFS= read -r g; do
  [ -n "$g" ] && aws logs delete-log-group --log-group-name "$g"
done < /tmp/orphans.txt
```

Show the participant the list before the delete loop. Log storage is cheap, so
leaving them is a valid choice — silently deleting the wrong ones is not.

## Step 6 — what deliberately stays, and say so

Do not delete these quietly, and do not report the account as clean without
naming them:

- **`CDKToolkit` and its staging bucket** — shared with anything else in the
  account using CDK.
- **Transaction Search stays enabled account-wide, by design.** It changes
  span-ingestion pricing and other workloads may have come to depend on it. It is
  the one thing a teardown does not undo, and it is an **account-scoped** change
  someone should know about.
- **Secrets Manager entries** may be in a recovery window rather than deleted.
- **ECR images** persist if the repository was not part of a deleted stack.

## Step 7 — report

State: exit code, stacks deleted, stacks remaining and why, whether any hourly
meter is still running, orphans found and whether they were removed, and what was
deliberately left. If anything is still standing, give the retry command and the
time to retry it — do not leave a partial teardown described as done.

If a stack is waiting on ENI drain, say plainly: nothing is billing hourly, retry
the delete in about 8 hours, and here is the command.
