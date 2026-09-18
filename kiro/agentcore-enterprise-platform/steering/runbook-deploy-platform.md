# Deploy the platform, end to end

Read this when someone wants to actually deploy the platform (a profile walk, a
workshop run, or a first build) rather than ask how it works.

**This is a runbook, not reference material.** Follow it in order. Anything that
creates, changes, deletes or bills goes one command at a time: state what it
does and what it costs, then stop and wait for approval. Never group one of
those with anything else. The halt conditions at the end are not advisory.

You are driving a real deployment that spends real money in someone's AWS
account. The participant approves every command. Your job is to make each
approval an informed one and to never let a broken layer be built on.

## The rule that governs every step

**Anything that creates, changes, deletes or bills goes one command at a time.**
For each of those state:

1. The exact command, copy-pasteable, with the working directory if it is not
   the repo root.
2. What it creates or changes — resources, not adjectives.
3. Its cost posture: `read-only` / `pay-per-use` / **`starts an hourly meter`**.
4. How long it should take, so silence is not mistaken for a hang.

Local read-only checks — `pwd`, a `--version`, an SSM read — can go in one group.
Splitting five harmless reads across five approvals teaches the participant to
click without reading, which is the opposite of what the approvals are for. Two
things stand alone even though they are read-only: `aws sts get-caller-identity`,
because its output is a go/no-go you have to confirm out loud, and any command
whose result decides what you propose next.

Never put a resource-creating command in a group, and never pair one with a read
— that is how something billable gets approved on the strength of the harmless
command next to it. Never run a billable command to learn something a read-only
call answers. If the participant says "just run everything", say once that you
will still stop before each command that creates resources, and continue — the
approvals are the point of this runbook.

## If you cannot run commands yourself

Some sessions have no shell — the tool is unavailable, or there is nobody present
to approve anything. Say so plainly, say that nothing has run and nothing was
created, and hand over the sequence for the participant to run themselves.

When you do, **keep every `deploy` on its own line and never chain it to
anything with `&&`.** A handed-over block gets pasted whole, and a chain of
`deploy && verify` pairs then walks the entire profile unattended — which is
precisely the go/no-go this runbook exists to enforce, deleted. One command per
line, the verify under it, and a blank line and a comment at each module
boundary so the person can stop there.

Say what you could not establish, too. An unverified prerequisite is not a
passed one: if you never ran `aws sts get-caller-identity`, the account is
unknown, and that is the fact to report rather than an assumption to build on.

## Before the first command: two warnings that must be said out loud

**Every `deploy` prints the Cognito M2M client secret to stdout in plaintext,
once the `-auth` stack exists — including runs that never touch `-auth`.** The
closing summary dumps the outputs of every prefix-matching stack, and that
secret is a CloudFormation export. **No `deploy` is screen-safe.** If this is a
workshop, a demo, or any shared screen, say this before the first deploy and let
them decide whether to stop sharing.

**`--dry-run` only works on `workshop`.** The `deploy` action parses the flag and
never reads it, so `deploy --module 3 --dry-run` bootstraps the Region and
deploys for real. Never propose `deploy` with `--dry-run`; it reads as safe and
is not.

## Phase 0 — establish where you are (all read-only)

Do not assume the checkout, the Region, or the account. Where you are is one
group:

```bash
pwd && git -C . rev-parse --short HEAD    # confirm the accelerator checkout
echo "${AWS_REGION:-<unset>}"             # unset means the CLI default, not us-east-1
```

Which account is its own proposal, because its answer is a go/no-go rather than
a fact to note:

```bash
aws sts get-caller-identity
```

Stop and confirm the account id and Region with the participant before going
further — deploying into the wrong account is the one mistake here with no undo.
If `AWS_REGION` is unset, propose setting it explicitly rather than relying on a
profile default:

```bash
export AWS_REGION=us-east-1
```

## Phase 1 — preflight (read-only)

Each of these has produced a failed session. Check them before deploying, not
after a failure.

```bash
python3.13 --version        # must exist under exactly this name
bash --version              # must be 4+; macOS /bin/bash is 3.2 and dies on declare -A
node --version && npm --version
npx cdk --version           # must be current; see below
aws bedrock list-foundation-models --query "modelSummaries[?contains(modelId,'claude')].modelId" --output text
```

Two of these need interpretation rather than a pass/fail glance:

- **The CDK CLI.** `requirements.txt` pins no upper bound on `aws-cdk-lib` and
  the repo has no `package.json`, so pip installs the newest library while `npx`
  uses whatever global CLI exists. A stale CLI fails at bootstrap with a
  cloud-assembly schema mismatch. The accelerator's own prereq check prints
  `✓ cdk` for any version — it only tests presence. If in doubt, propose
  `npm install -g aws-cdk@latest`.
- **Bedrock model access.** An empty list here means module 6's first invoke will
  fail with an access error ~8 minutes into the build. Fix it now, in the Bedrock
  console, not then.

Then the virtual environment:

```bash
python3.13 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

`source` does not persist between your tool calls. Prefix later Python commands
with the venv interpreter (`.venv/bin/python scripts/…`) rather than assuming an
activated shell.

## Phase 2 — preview the whole plan for free

This makes **zero AWS calls** — no credential check, no bootstrap. Always do it
before committing to a profile.

```bash
./scripts/deploy.sh workshop --dry-run --profile <profile>
```

Confirm from the output, not from what you typed. A real dry run prints:

```
[INFO]  Dry run: skipping prerequisite and credential checks (no AWS calls)
[INFO]  DRY RUN — nothing will be deployed
```

and never reaches a `═══ Deploying ═══` header. If you see that header, it is
deploying — stop it.

One exception: `--profile security-focused` hits the `ORG_ID` gate *before* the
plan prints, even in dry-run. Export any `o-…`-shaped value just to preview, or
the real one:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

If the account is not in an Organization, say so and steer to another profile or
`enable_resource_policies=false` — do not let them discover it mid-walk.

Read the printed sequence back against the profile they chose. If it does not
match `steering/deploy.md`, trust the output and say the docs are stale.

## Phase 3 — the cost conversation, before anything bills

Say this before the first billable command, in meters rather than dollars
(rates vary by Region and change):

- `greenfield`, `migration`, `multi-agent` leave **nothing** billing hourly.
- `platform-team` and `security-focused` enable networking, which creates **1 NAT
  gateway plus 5 interface endpoints across 2 AZs = 10 endpoint-AZ-hours**,
  billing whether or not anything runs. There is **no flag** to reduce this —
  `enable_vpc_endpoints=True` is a literal at the call site (`app.py:197`) and
  `max_azs=2` is hardcoded.
- **Transaction Search defaults on** and changes span-ingestion pricing
  **account-wide**, and it **stays on after teardown** by design. Add
  `ENABLE_TRANSACTION_SEARCH=false` if a platform team owns tracing elsewhere —
  but then module 9's tracing genuinely does not work, so make it a decision, not
  a default.
- CodeBuild bills per container build, and every agent-pattern swap is another one.

Then ask for an explicit go, and ask when they intend to tear down. A platform
that nobody agreed to destroy is the one still running next month.

## Phase 4 — bootstrap the Region

```bash
npx cdk bootstrap
```

Creates the `CDKToolkit` stack and its staging bucket. One-time per
account/Region, ~1–2 min, pay-per-use and negligible. Note that this is shared
infrastructure — do **not** delete it at teardown if anything else in the account
uses CDK.

## Phase 5 — the module loop

Walk the profile's sequence **in the profile's order, not numeric order**.
`platform-team` deliberately runs A before 6, because the orchestrator runtime
depends on the memory stack.

| Profile | Sequence |
|---|---|
| `greenfield` | 3 4 5 6 9 |
| `migration` | 3 4 6 7 9 |
| `multi-agent` | 3 4 5 6 7 8 9 |
| `platform-team` | 3 4 5 A 6 7 8 9 C E |
| `security-focused` | 3 4 5 6 9 E |

For each module, three proposals in order — deploy, verify, then a go/no-go.

**Step 1, deploy.** Scope it with `--module`, never with `--profile` alone:
`--profile` materializes that profile's preset as `platform.yaml` and then runs
`cdk deploy --all`, which deploys everything the manifest declares.

```bash
./scripts/deploy.sh deploy --module <N>
```

Two modules need their flag exported on the standalone `deploy --module` path,
because only module 8 turns its own flag on:

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module C
ENABLE_SECURITY=true   ./scripts/deploy.sh deploy --module E
```

Without them you get `No stacks match the name(s) …-networking`, and the error's
own advice to check the CloudFormation console is a dead end — the stack was
never synthesized.

**Step 2, verify.** `CREATE_COMPLETE` proves resources exist, not that anything
works. Stacks have completed while the thing they promise was broken; that is why
these scripts exist.

| Module | Stack(s) | Verify | Expect |
|---|---|---|---|
| 3 | `$PREFIX-auth` | `aws ssm get-parameter --name /agentcore-workshop/dev/auth/issuer-url` | ~2 min |
| 4 | `$PREFIX-auth` `$PREFIX-identity` | `aws ssm get-parameter --name /agentcore-workshop/dev/identity/gateway-credential-provider-name` | ~2 min |
| 5 | `$PREFIX-gateway` | `.venv/bin/python scripts/test_gateway.py` | ~3 min |
| A | `$PREFIX-memory` | `.venv/bin/python scripts/test_memory.py` | ~2 min |
| 6 | `$PREFIX-runtime-orchestrator` | `.venv/bin/python scripts/invoke.py "Reply with exactly: WORKSHOP OK"` | **~7–8 min** |
| 7 | `$PREFIX-gateway` | `test_gateway.py`, then `invoke.py --tools` | ~3 min |
| 8 | `$PREFIX-runtime-code-agent` `-research-agent` | `.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"` | ~8 min |
| 9 | `$PREFIX-observability` | `.venv/bin/python scripts/check_observability.py` | ~3 min |
| C | `$PREFIX-networking` | `.venv/bin/python scripts/check_network.py` | ~5 min |
| E | `$PREFIX-security` | stack COMPLETE only | ~3 min |

`$PREFIX` is `${PROJECT_NAME}-${ENVIRONMENT}`, default `agentcore-workshop-dev`.
**SSM paths do not use `$PREFIX`** — they are `/$PROJECT_NAME/$ENVIRONMENT/…`,
slash-separated, so the literal paths above are correct only at the defaults. If
`PROJECT_NAME` or `ENVIRONMENT` was overridden, substitute rather than pasting.

**Step 3, go/no-go.** Report the module, its wall-clock time, and its verify
result. Then ask whether to continue. A failed verify is information — read
`steering/troubleshooting.md` for the symptom and fix the layer before building
the next one on top of it.

### Three things to volunteer during the loop, not after

- **Module 6 going silent for 7–8 minutes is not a hang.** CodeBuild is building
  an arm64 container image remotely. Say this *before* starting module 6, not
  when someone asks if it is stuck.
- **The default `orchestrator` agent has no tools and no memory.** Asking it
  "what tools do you have?" correctly returns nothing — tools live on the gateway
  (`invoke.py --tools`). It never reads the `MEMORY_ID` it is given, so a
  same-session recall demo fails even with Memory `ACTIVE`. For either demo,
  deploy `strands-agent` or `langgraph-agent` instead.
- **Module 9's verify usually fails the first time in a new account.** Enabling
  Transaction Search is asynchronous and outlasts the stack that requested it.
  Check `aws xray get-trace-segment-destination`, wait, re-run — it passes.

## Phase 6 — closing report

State plainly: which modules deployed, which verifies passed, total wall-clock,
what is billing hourly right now, and the teardown command. If any verify failed
or was skipped, say so explicitly — do not report a partial walk as a success.

Then remind them of the teardown decision from Phase 3, and offer the
`teardown-platform` runbook.

## Halt conditions — stop and report, do not work around

- `aws sts get-caller-identity` shows an account the participant did not expect.
- A verify fails and the participant has not chosen to continue anyway.
- The same stack fails twice in a row for the same reason.
- Anything asks you to put a secret in `platform.yaml`, `workshop.env`, or a
  `-c` context flag. The accelerator passes Secrets Manager **names** only.
- You are about to type a resource id, ARN, or account id into a public file.
