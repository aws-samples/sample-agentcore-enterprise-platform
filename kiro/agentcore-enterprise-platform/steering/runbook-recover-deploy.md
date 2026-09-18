# Recover a failed or interrupted deploy

Read this when a module died mid-walk, a stack is stuck in a rollback state, a
run was cancelled, or someone is about to redeploy everything to fix one layer.

**This is a runbook, not reference material.** Follow it in order. Anything that
creates, changes, deletes or bills goes one command at a time: state what it
does and what it costs, then stop and wait for approval. Never group one of
those with anything else. The halt conditions at the end are not advisory.

The instinct after a failure is to tear down and start again. That is usually
wrong here: CDK picks up from current stack state, and a resumed walk costs
seconds rather than the ~38 minutes of a full rebuild. Diagnose first.

## The rule

**Propose one command at a time and wait.** Diagnosis is entirely read-only —
work through it before proposing anything that deploys or deletes. Do not delete
a stack to "clean up" until you have established it cannot be deployed onto.

## Step 1 — what state is everything in (read-only)

```bash
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'agentcore-workshop-dev')].[StackName,StackStatus]" \
  --output table
```

The status decides the whole approach:

| Status | Meaning | Action |
|---|---|---|
| `CREATE_COMPLETE` / `UPDATE_COMPLETE` | fine | move on |
| `UPDATE_ROLLBACK_COMPLETE` | update failed, previous version intact | **safe to deploy onto again** — just re-run |
| `ROLLBACK_COMPLETE` | initial create failed; never existed successfully | must be **deleted** before it can be recreated |
| `*_IN_PROGRESS` | still working | **wait** — do not start a second deploy |
| `DELETE_FAILED` | see the teardown-specific causes below | usually a stabilization timeout, not real |
| `UPDATE_ROLLBACK_FAILED` | rollback itself failed | needs `continue-update-rollback` |

Then get the actual cause — the console link in the CLI output is rarely the
fastest path:

```bash
aws cloudformation describe-stack-events --stack-name <stack> \
  --query "StackEvents[?ResourceStatus=='CREATE_FAILED'||ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
  --output table
```

Read the **first** failure chronologically, not the last. CloudFormation reports
the cascade too, and the last event is usually a consequence.

## Step 2 — the failures that are not what they say

Check these before treating a message at face value:

**`No stacks match the name(s) …-networking` / `…-security`.** The stack was never
synthesized because the feature flag was off — nothing failed, and there is
nothing in the console to look at. Re-run with the flag:

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module C
ENABLE_SECURITY=true   ./scripts/deploy.sh deploy --module E
```

**A cloud-assembly schema mismatch at bootstrap.** The global CDK CLI is older
than the `aws-cdk-lib` pip installed. The prereq check passes it anyway because it
only tests that `cdk` exists. Fix: `npm install -g aws-cdk@latest`.

**A model access error ~8 minutes into module 6.** No Claude model is enabled in
Bedrock in this Region. The build succeeded; the first invoke did not.

**`ResourceNotFoundException` mentioning a Legacy model.** The hardcoded model id
aged out. The message says "Access denied" but access is not the problem — set
`MODEL_ID` to a current model.

**`declare -A: invalid option`.** Someone ran it under macOS `/bin/bash` 3.2.
`brew install bash`, then invoke as `bash scripts/deploy.sh`.

**A "dry run" that deployed.** `--dry-run` is only read by `workshop`. On `deploy`
it is parsed and ignored. If a `deploy --dry-run` created resources, that is the
cause, and the resources are real — treat this as an unplanned deploy and check
what landed, including a `CDKToolkit` stack in an unintended Region.

**A misspelled flag that deployed everything.** The parser ends in `*) shift ;;`,
so `--modul 6` drops the flag and deploys the whole app. Check the blast radius
against what they intended before deciding what to remove.

For anything not listed, route by symptom in `steering/troubleshooting.md` — it is
indexed by what the participant sees.

## Step 3 — clear the state that blocks a retry

Only for `ROLLBACK_COMPLETE`, which cannot be deployed onto:

```bash
aws cloudformation delete-stack --stack-name <stack>
aws cloudformation wait stack-delete-complete --stack-name <stack>
```

For `UPDATE_ROLLBACK_FAILED`:

```bash
aws cloudformation continue-update-rollback --stack-name <stack>
```

State plainly what deleting the stack destroys before proposing it. Deleting
`-auth` invalidates every downstream reference to the Cognito pool and forces a
rebuild of the layers above it — that is a much larger action than it looks.

## Step 4 — resume, do not restart

If the walk died partway through a profile sequence, resume at the failed module:

```bash
./scripts/deploy.sh workshop --from 6
```

`--from` is validated against the selected profile's sequence and fails with the
full sequence printed if the module is not in it. Skipped modules are logged, one
`Skipping module N (--from 6)` line each — not silently dropped.

**A resumed run reprinting earlier stacks is not a re-deploy.** Module 6's
`cdk deploy` names its dependencies, so `--from 6` walks `-auth`, `-identity` and
`-gateway` on the way through. Each returns `✅ <stack> (no changes)` with
`Deployment time: 0s` — about 18 seconds of detour in a measured run. Read the
`(no changes)` before concluding `--from` was ignored, and say this in advance so
nobody panics at seeing module 3 scroll past again.

If a single module failed rather than the walk, re-run just that module —
`deploy --module <N>` — after fixing the cause.

## Step 5 — confirm the recovery

Re-run the failed module's verify script, not just the deploy. A stack that
reaches `CREATE_COMPLETE` on the retry can still be broken in the way that caused
the original failure. The `verify-platform` runbook has the per-layer matrix.

## What not to do

- **Do not tear down and rebuild to fix one layer.** It costs ~38 minutes, and if
  the cause was a prerequisite (CDK version, model access, bash version) the
  rebuild fails the same way at the same place.
- **Do not run a second deploy while one is `*_IN_PROGRESS`.** You get a
  confusing failure on top of a working deploy.
- **Do not delete `CDKToolkit`.** It is shared with anything else in the account
  using CDK.
- **Do not retry more than twice for the same reason.** Two identical failures
  means the cause is upstream of the command. Stop and report.
