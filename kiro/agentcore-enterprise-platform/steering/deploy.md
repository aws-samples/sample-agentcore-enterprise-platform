# Deploying

Read this when choosing how to run the accelerator, scoping a deploy, resolving
where a config value came from, or tearing down.

All commands run from the root of a `sample-agentcore-enterprise-platform`
checkout, with `.venv` created and `pip install -r requirements.txt` done.
`AWS_REGION` and credentials must be set for anything that touches AWS.

---

## The four ways to run it

```bash
./scripts/deploy.sh workshop                 # guided: explain → deploy → verify → pause, per module
./scripts/deploy.sh deploy --module 5        # one module's stacks
./scripts/deploy.sh deploy --team agent      # one team's stacks
./scripts/deploy.sh deploy --profile greenfield   # profile FLAGS + cdk deploy --all
```

`workshop` is the right default for a first pass or a facilitated session: it
narrates each module, deploys it, runs its verify, and waits for you before
moving on.

### A profile is a preset file, and `--profile` writes it to disk

`--profile X` copies `presets/X.yaml` over `platform.yaml` — the durable
deployment manifest — before the config is loaded, so it participates with the
normal precedence (env > `platform.yaml` > `workshop.env`). `materialize_preset`
at `scripts/deploy.sh:107-129` does the copy; the argv pre-scan at
`scripts/deploy.sh:134-146` is what makes it happen early.

Three things follow, and all three surprise people:

- **It runs on every action except `config`, and is suppressed only by
  `--dry-run`.** So `ls --profile greenfield` is *not* a read-only measurement
  any more — it rewrites `platform.yaml`. Verified.
- **The intent is now durable.** `deploy --profile greenfield` followed by a
  plain `deploy` keeps greenfield's posture, because the manifest is still on
  disk. Previously the flags died with the run and the plain re-run quietly
  picked the app's own defaults back up — A2A among them, which is two extra
  runtimes and their CodeBuild projects.
- **A hand-edited manifest is protected.** `platform.yaml` is regenerable only
  while it carries the generated-from header. Remove the header (or write the
  file yourself) and `--profile` refuses rather than overwriting:
  `Refusing to overwrite a hand-edited manifest`. `--yes` overrides, with a
  warning. Dropping `--profile` is usually the right answer — the file already
  *is* the config.

### The `--profile` scope trap

Scope is a separate question from posture, and `--profile` still does not narrow
it: with no `--stack`/`--team`/`--module`, the stack list is **empty**, which
means `cdk deploy --all` (`scripts/deploy.sh:1067-1078`) — every stack the
manifest makes the app synthesize, not just the modules in that profile's
sequence. `PROFILE_MODULES` (`scripts/deploy.sh:267-276`) is read by the
`workshop` action and nothing else.

```bash
./scripts/deploy.sh deploy --profile platform-team     # deploys EVERYTHING
./scripts/deploy.sh workshop --profile platform-team   # walks 3 4 5 A 6 7 8 9 C E
./scripts/deploy.sh deploy --profile platform-team --module 5   # manifest + just the gateway
```

Interactively this is no longer silent: before `--all`, `confirm_footprint`
(`scripts/deploy.sh:658-679`) prints the account, region, prefix, which config
source won, and the full `cdk ls` output, then blocks on `Proceed to deploy ALL
of the above? [y/N]`. **`--yes` and `NON_INTERACTIVE=1` both skip that prompt**,
so the trap is fully live on the CI path the README documents.

**To measure the footprint without deploying and without writing the manifest,
ask the contract instead of the CLI:**

```bash
.venv/bin/python -c "
from infra_utils.platform_config import load_platform_config
c = load_platform_config('presets/greenfield.yaml')
print(len(c.expected_stacks()), c.expected_stacks())"
```

`expected_stacks()` (`infra_utils/platform_config.py:295`) is the deployment
contract — deploy plans, verification, the dashboard and destroy all consume it,
and `scripts/check-contract.sh` fails CI if it drifts from what `app.py` actually
synthesizes. Measured across the five presets:

| `--profile` | Stacks `deploy --all` would create | Overshoot vs the profile's modules |
|---|---|---|
| `greenfield` | 6 — auth, identity, **memory**, gateway, runtime-orchestrator, observability | `-memory` is module **A**, which greenfield never walks |
| `migration` | 6 — same six | `-memory` again; module 5 (gateway) is not in the sequence either |
| `multi-agent` | 8 — the six plus code-agent and research-agent | `-memory` |
| `platform-team` | 10 — networking, security, auth, identity, memory, gateway, 3 runtimes, observability | none; it is the whole app |
| `security-focused` | 8 — networking, security, auth, identity, memory, gateway, orchestrator, observability | `-memory` |

Two nuances worth being precise about, because overstating this costs credibility:

- **The manifest does gate the expensive stacks.** `greenfield` leaves
  `security: {}`, and `security.networking` defaults to `false`
  (`infra_utils/platform_config.py:179`), so `-networking` is not in the app at
  all and `deploy --profile greenfield` cannot create a NAT gateway. The
  overshoot is Memory, which is cheap.
- **What you actually lose is the walk**, not money: no per-module pause, no
  verify step between layers, and one failure anywhere in `--all` leaves you
  diagnosing a partially-deployed platform instead of a single module.

---

## Actions and flags

| Action | Does |
|---|---|
| `deploy` | deploy the selected stacks |
| `workshop` | guided module-by-module walk of a profile's sequence |
| `verify` | run every check this configuration promises; **exits non-zero on failure** (`verify.md`) |
| `destroy` | destroy selected stacks; no selection means `--all` |
| `synth` | `cdk synth` with the resolved context |
| `diff` | `cdk diff` with the resolved context |
| `export` | write `workshop-outputs-<stamp>.json` (SSM params + stack outputs) |
| `ls` / `list` | `cdk ls` — the stacks the app currently defines |
| `config` | print saved answers |
| `config --reset` | delete saved answers |

`verify` is missing from the script's own one-line `Usage:` string
(`scripts/deploy.sh:1177`) while being described in the `Actions:` block two lines
below it. The action works; do not conclude from `--help` that it does not exist.

| Flag | Applies to | Does |
|---|---|---|
| `--stack NAME` | deploy, destroy, synth, diff | one named stack |
| `--profile P` | any action but `config` | **writes `presets/P.yaml` to `platform.yaml`**; also picks the sequence for `workshop` |
| `--team T` | deploy | `platform` \| `agent` \| `security` |
| `--module N` | deploy | `3 4 5 6 7 8 9 A B C D E` |
| `--from M` | workshop | skip forward to module M in the sequence |
| `--dry-run` | workshop **only** | print the plan; **zero AWS calls**, and the one thing that suppresses materialization. Accepted and silently ignored by `deploy` — see below |
| `--yes` | deploy, destroy | skip the full-footprint confirmation; overwrite a hand-edited `platform.yaml`; make the post-destroy sweep delete rather than ask |

`NON_INTERACTIVE=1` skips every prompt — required for CI, and it turns some
prompts into hard failures (see ORG_ID below). It implies `--yes` for the
footprint confirmation but **not** for the post-destroy sweep, which reports and
leaves instead (`runbook-teardown-platform.md`).

### The parser fails closed — spelling is no longer the risk

This used to be the sharpest edge in the script: the argument loop ended in
`*) shift ;;`, so an unrecognised flag was dropped without a warning, which left
the stack list empty, and empty meant `cdk deploy --all`. A typo produced a
*larger* deployment than the one you asked for.

Upstream closed it (`scripts/deploy.sh:955-990`). Verified, all five exit 1:

| Typed | Now |
|---|---|
| `deploy --dryrun` | `Unknown option or argument: '--dryrun'` + the valid list |
| `deploy --modul 6` | same |
| `deploy --stack=identity` | same, plus `values are space-separated` |
| `deploy --team platfrom` | `Unknown team: 'platfrom'. Valid teams: security agent platform` |
| `deploy --profile greenfeld` | `Unknown profile: 'greenfeld'. Valid profiles: …` |

`--profile` and `--team` values are now validated for **every** action, not just
`workshop`, and `require_flag_value` catches a flag given no value at all. The
asymmetry that used to matter — unknown *values* rejected, unknown *flag names*
ignored — is gone.

Confirm you are in dry-run from the output, which says so explicitly, rather than
from what you typed:

```
[INFO]  Dry run: skipping prerequisite and credential checks (no AWS calls)
[INFO]  DRY RUN — nothing will be deployed
```

If those lines are missing, you are deploying.

### `deploy --dry-run` is not a dry run — it deploys

Now that spelling fails closed, this is the surviving trap in the flag parser —
and the nastier one, because you can type it perfectly and still be wrong.
`--dry-run` is parsed globally, but only the `workshop` action ever reads it. The
`deploy` case (`scripts/deploy.sh:1052-1081`) checks `NON_INTERACTIVE`, then calls
`cdk_bootstrap` and `deploy_stacks` with **no `DRY_RUN` test anywhere** — compare
`workshop`, where every one of those steps is guarded.

Measured, and this is what it cost: `deploy --module 3 --dry-run` pointed at an
untouched Region ran to `Total time: 58.51s` and left two real stacks standing —
`CDKToolkit` (the bootstrap: staging bucket, ECR repo, five IAM roles) and
`-auth` (a live Cognito user pool, three app clients, a hosted domain). It printed
the deployment summary table, and with it the M2M client secret. Nothing warned.

```bash
# preview a deploy without deploying:
./scripts/deploy.sh workshop --dry-run --profile greenfield   # free, safe
./scripts/deploy.sh synth  --module 3                         # renders templates, no deploy
./scripts/deploy.sh diff   --module 3                         # needs an existing stack
```

`synth` and `diff` do run the credential check (they call CDK), but neither
creates anything. There is no dry run for `deploy`; use `workshop` when you want
one.

### `workshop --dry-run` really is free

`workshop --dry-run` skips the prerequisite check, the credential check, the
venv check, and CDK bootstrap entirely (`scripts/deploy.sh:819-826` — the skip is
conditional on `ACTION = workshop`, which is why `deploy` does none of it). It
prints, per module, the stacks it would deploy and the exact verify command it
would run. Use it to preview a profile, to confirm a config change landed, and as
the first thing in any session.

**One exception, and it bites when you are preparing:** the `ORG_ID` gate runs
*before* the plan is printed and applies in dry-run too. So
`workshop --dry-run --profile security-focused` produces no plan at all unless
`ORG_ID` is set — with a terminal it stops and prompts, and with stdin closed it
warns and exits 1. To preview that profile's plan without an Organizations id,
any `o-…`-shaped value works, because dry-run never calls AWS:

```bash
ORG_ID=o-preview0 ./scripts/deploy.sh workshop --dry-run --profile security-focused
```

Use a real id for anything that actually deploys.

### Resuming

```bash
./scripts/deploy.sh workshop --from 6
```

`--from` is validated against the selected profile's sequence and fails with the
full sequence printed if the module is not in it. Modules before it are logged
as skipped, not silently dropped — you get one `Skipping module N (--from 6)` line
each.

**A resumed run still prints the earlier stacks, and that is not a re-deploy.**
Module 6's `cdk deploy` names its dependencies, so `--from 6` walks `-auth`,
`-identity` and `-gateway` on the way through. Each comes back
`✅ <stack> (no changes)` with `Deployment time: 0s`; the whole detour cost ~18
seconds in a real run. Read the `(no changes)` before concluding that `--from` was
ignored.

If a deploy died mid-stack, just re-run the same command — CDK picks up from the
current stack state, and a stack in `UPDATE_ROLLBACK_COMPLETE` is safe to deploy
onto again.

---

## Profiles

Each profile is two things: a **flag set** and a **module sequence**. `deploy`
uses only the flags; `workshop` uses both.

| Profile | Flags it sets | Sequence |
|---|---|---|
| `greenfield` | `enable_networking=false enable_security=false enable_a2a=false` | 3 4 5 6 9 |
| `migration` | `enable_networking=false enable_security=false enable_a2a=false` | 3 4 6 7 9 |
| `multi-agent` | `enable_networking=false enable_security=false enable_a2a=true` | 3 4 5 6 7 8 9 |
| `platform-team` | `enable_networking=true enable_security=true enable_a2a=true` | 3 4 5 A 6 7 8 9 C E |
| `security-focused` | `enable_networking=true enable_security=true enable_a2a=false enable_resource_policies=true enable_egress_filter=true enable_cedar=true enable_traceability=true` | 3 4 5 6 9 E |

Two orderings that look wrong and are deliberate:

- **`platform-team` runs A (Memory) before 6 (Agent Deployment).** The
  orchestrator depends on memory (`app.py`: `runtime_orchestrator.add_dependency(
  memory_stack)`). If 6 ran first, CDK would create memory implicitly and module A
  would report "no changes" — teaching the room something false about what they
  just built.

  The corollary matters for **every** profile: a `-memory` stack and a live Memory
  resource land at module 6 whether or not A is in the sequence. A verified
  `greenfield` run leaves **six** stacks for five modules — `-auth`, `-identity`,
  `-gateway`, **`-memory`**, `-runtime-orchestrator`, `-observability` — and
  `deploy.sh verify` duly runs `test_memory.py` against it. Module A is where Memory gets
  *introduced and verified*, not where it comes from. Count it in teardown.
- **`security-focused` sets `enable_a2a=false`.** It is about the control plane,
  not the agent fleet.

`migration` skips module 5 and reaches the gateway through module 7 instead —
the point being that an existing agent gets governed by adding tool targets, not
by rebuilding.

### `security-focused` needs an Organizations ID

`enable_resource_policies=true` renders `aws:PrincipalOrgID` into the Memory
resource policy, so it cannot synthesize without one.

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

Behaviour when it is missing:

- **Interactively** the script *prompts* for an `o-xxxx` value and only fails if
  you leave it empty.
- **With `NON_INTERACTIVE=1`** it is a hard stop.

Separately, `enable_networking=true` without `ORG_ID` does **not** fail — it
warns and creates the AgentCore VPC endpoint with **no policy at all**. That is
a quiet loss of a control, so treat the warning as an error in any environment
that matters.

---

## Teams

For a session split across workstreams:

| Team | Stacks (`PREFIX` = `${PROJECT_NAME}-${ENVIRONMENT}`) |
|---|---|
| `platform` | `-networking -auth -identity -gateway -observability` |
| `agent` | `-runtime-orchestrator -runtime-code-agent -runtime-research-agent -memory` |
| `security` | `-security -observability` |

Note `-observability` is in both `platform` and `security` — deliberate overlap,
and harmless because CDK is idempotent.

---

## Where config values come from

Precedence, highest first:

1. **Explicit environment variables** — `AWS_REGION=…`, `AGENT_PATTERN=…`
2. **`platform.yaml`** — applied before saved answers are loaded
3. **`workshop.env`** — answers saved by a previous interactive run
4. **Interactive prompts**, then in-code defaults

Ten keys are persisted to `workshop.env`: `AWS_REGION`, `IDP_TYPE`,
`IDP_TENANT_ID`, `IDP_CLIENT_ID`, `IDP_ISSUER_URL`, `MODEL_ID`, `ORG_ID`,
`PROJECT_NAME`, `ENVIRONMENT`, `AGENT_PATTERN`. **Secrets are never persisted.**

Defaults: `PROJECT_NAME=agentcore-workshop`, `ENVIRONMENT=dev`,
`AWS_REGION=us-east-1`. So the default stack prefix is `agentcore-workshop-dev`.

When answers seem stale — and they will, because the wizard remembers:

```bash
./scripts/deploy.sh config          # effective values, with their source
./scripts/deploy.sh config --reset  # delete workshop.env
```

`config` needs no credentials and makes no AWS calls. It prints a
`platform.yaml → effective values` block if that file exists, then the raw
contents of `workshop.env` if that exists — and on a fresh checkout, where
neither does, it prints exactly `none`. That is the correct answer, not a failure.

`config --reset` deletes **`workshop.env` only**. It does not touch
`platform.yaml`, so if a value survives a reset, that file is where it lives.

### `platform.yaml`

Declare the whole deployment in one file instead of environment variables:

```yaml
project: acme-agents
environment: dev
region: us-east-1
deployment:
  strategy: centralized       # centralized | distributed | federated
identity:
  idp: cognito
agents:
  pattern: langgraph-agent
  a2a: true
  memory:
    long_term: true
gateway:
  web_search: auto
  tools: [sample-tool]
security:
  networking: true
  cloudtrail_alerting: true
  traceability: true
observability:
  transaction_search: true
```

`presets/` holds one starting file per profile (`greenfield.yaml`,
`migration.yaml`, `multi-agent.yaml`, `platform-team.yaml`,
`security-focused.yaml`). Validate offline before deploying:

```bash
.venv/bin/python -m infra_utils.platform_config platform.yaml
```

Validation is deliberately strict and reports **every** problem at once, before
any AWS call. **Unknown keys are errors, not no-ops** — a typo would otherwise
silently do nothing. An invalid file hard-stops the deploy.

Secrets never go in this file: it holds Secrets Manager *names*, never values.

Two facts about the file itself, both easy to trip over:

- **It is gitignored.** It is per-deployment state, not source. If you want to
  commit a team's manifest, `git add -f`.
- **`--profile` regenerates it,** but only while the generated-from header is
  intact. Delete the header to protect your edits; then `--profile` refuses
  instead of overwriting, and `--yes` is the override.

### Adding your own stack: the use-case extension point

There is a first-class seam for this now — you do not need to patch `app.py`.
A use case is a directory under `use-cases/<name>/` with a `manifest.yaml`, and
the manifest is the only file the platform reads about it
(`infra_utils/platform_config.py:213-249`):

```yaml
name: hello-platform        # must equal the directory name
owner: <alias or team>      # who reviews changes to this folder
summary: <one line>
requires: [gateway]         # core stack suffixes that must be in the footprint
stacks: [uc-hello-platform] # suffixes this adds; the uc- prefix is enforced
entry: stack.py             # module exposing build(app, platform_ctx, config)
```

Enable it by listing the name under `use_cases:` in the manifest. Four guardrails
worth knowing, because each one is a hard error rather than a surprise later:

- `stacks:` suffixes **must** start with `uc-`, so a contribution cannot collide
  with or masquerade as a core stack.
- `requires:` is checked against the footprint **per federation role**, so a
  gateway-requiring use case cannot select itself into a federated workload
  account that has no local gateway.
- `extra: forbid` on the manifest model — an unknown key fails to load.
- A broken manifest is a hard error naming the file; a contribution never
  half-loads.

`use-cases/hello-platform/` is the reference implementation and the thing to copy.
`CONTRIBUTING_USE_CASES.md` and `docs/PLATFORM_INTERFACE.md` are the contract.
This is the answer to "how do we add our own integration without forking" —
previously that meant reading platform values out of SSM and deploying a separate
app, which still works and is described under **Multi-account** below.

---

## Feature flags

Set as CDK context (`-c flag=value`) or as an environment variable
(`FLAG=value`, uppercased).

| Flag | Env | Default | Effect |
|---|---|---|---|
| `agent_pattern` | `AGENT_PATTERN` | `orchestrator` | which agent framework builds |
| `enable_a2a` | `ENABLE_A2A` | true at app level; profiles override | A2A sub-agent stacks exist at all |
| `enable_networking` | `ENABLE_NETWORKING` | false | VPC + private subnets + AgentCore endpoints; runtimes get `network_mode: VPC` |
| `enable_security` | `ENABLE_SECURITY` | false | KMS CMK + CloudTrail; prerequisite for `enable_traceability` |
| `enable_resource_policies` | `ENABLE_RESOURCE_POLICIES` | false | Memory resource policy; **requires `org_id`** |
| `enable_egress_filter` | `ENABLE_EGRESS_FILTER` | false | Bedrock Guardrail + egress interceptor Lambda on the gateway |
| `enable_cedar` | `ENABLE_CEDAR` | false | Cedar policy engine on the gateway |
| `cedar_mode` | `CEDAR_MODE` | `LOG_ONLY` | `LOG_ONLY` or `ENFORCE` |
| `enable_traceability` | `ENABLE_TRACEABILITY` | false | SNS + EventBridge alerting on sensitive AgentCore API calls |
| `enable_transaction_search` | `ENABLE_TRANSACTION_SEARCH` | **true** | X-Ray trace segment destination → CloudWatch Logs. Account- and Region-scoped |
| `enable_web_search` | — | auto by Region | built-in web-search gateway connector |
| `use_long_term_memory` | — | false | long-term memory + semantic extraction (costs more) |
| `org_id` | `ORG_ID` | unset | `o-xxxx`, required by the resource policy and the VPCE policy |
| `model_id` | `MODEL_ID` | unset | Bedrock model override; unset means each pattern's in-code default |

Two flags that trip people up:

- **Flags are matched against the exact lowercase string `"true"`.**
  `-c enable_cedar=True` silently does nothing.
- **`enable_transaction_search` defaults to `true`** because without it every
  OTLP span batch is rejected with HTTP 400 while the deploy still reports
  success. It is an account+Region setting and **survives teardown** by design.

### The Region gate nobody expects

`enable_web_search` is auto-enabled only in `us-east-1`, `eu-west-1`, and
`ap-northeast-1`, and off elsewhere. Creating the target in an unsupported
Region **fails the deploy**, so the gate is what keeps other Regions working.
Any connector you add yourself needs the same treatment.

The interactive Region prompt offers: `us-east-1`, `us-west-2`, `eu-west-1`,
`eu-central-1`, `ap-northeast-1`, `ap-southeast-1`.

**Nothing stops you from deploying into a Region where AgentCore does not exist.**
The only Region check is `sts get-caller-identity`, which succeeds anywhere the
Region is enabled. Measured in `eu-west-3`: every prerequisite passed, the
prerequisite banner printed `Region: eu-west-3`, and module 3 deployed a real
Cognito pool in 58s. The failure would not have surfaced until a module that calls
the AgentCore control plane — modules 5, A, 6 — by which point identity is already
standing in the wrong Region. With `NON_INTERACTIVE=1` there is no prompt to catch
it either, so the Region comes from `AWS_REGION` unvalidated. Confirm first:

```bash
aws bedrock-agentcore-control list-gateways --region "$AWS_REGION" >/dev/null \
  && echo "AgentCore control plane responds in $AWS_REGION"
```

---

## Secrets

- The IdP client secret is written to Secrets Manager as
  `${PREFIX}-idp-client-secret` (override the name with
  `IDP_CLIENT_SECRET_NAME`). Only the **name** is passed to CDK, resolved with
  `{{resolve:secretsmanager:...}}`.
- The script strips whitespace from the pasted secret. This is not cosmetic — a
  trailing newline from a copy-paste produces `invalid_client` at the IdP, which
  is an hour of debugging that looks like a misconfiguration.
- API-key tool secrets are prompted for `tavily`, `google-search`, `google-maps`
  and stored as `${PREFIX}-<key>-api-key`.
- Nothing secret is ever written to `workshop.env` or `platform.yaml`.

---

## Prerequisites the script checks

`node`, `npm`, `python3.13`, `aws` are required. `docker` is reported as
**optional** and the run continues without it — container images build in AWS
CodeBuild.

The CDK CLI is probed with `npx --no-install cdk --version` and installed with
`npm install -g aws-cdk` if absent. The `--no-install` matters: a bare
`npx cdk --version` prompts "Ok to proceed?" and, with output suppressed and no
TTY, waits forever.

**The check tests presence, not compatibility** — and that gap is a real failure.
`requirements.txt` pins `aws-cdk-lib>=2.265.0` with no ceiling, so pip installs the
newest library, which emits the newest cloud-assembly schema. The repo ships no
`package.json` and no `node_modules`, so `npx --no-install` resolves the **global**
CDK CLI, which may be months old. The prereq check prints a green `✓ cdk: <old
version>`, the run proceeds, and bootstrap dies about ten seconds later on a schema
mismatch — under an error message that blames the account and IAM instead. Upgrade
the CLI before a session:

```bash
npm install -g aws-cdk@latest && cdk --version
```

See `troubleshooting.md` → "CDK bootstrap failed".

CDK bootstrap (`CDKToolkit`) runs automatically for the account/Region.

### The wall of deprecation warnings is normal

Every module prints a block of
`[WARNING] aws-cdk-lib.Stack#addDependency is deprecated` — nine at a time, twice
per module — plus, on current `aws-cdk-lib`, a `No cross-stack-reference strength
configured, defaulting to "strong"` annotation. Both come from the library, neither
affects the deploy, and both scroll past immediately before a successful stack.
Say so before it happens, or a room will read it as the deploy failing.

---

## Multi-account

Set by `deployment.strategy` in `platform.yaml`:

| | `centralized` | `distributed` | `federated` |
|---|---|---|---|
| Accounts | one | one per team, full copy | one platform + N workload |
| Gateway & tools | local | per-account | **shared** (platform account) |
| Cognito / IdP | local | per-account | **shared** (platform account) |
| Runtimes | local | per-account | workload accounts only |
| Memory | local | per-account | **per-workload** |
| Choose when | workshops, pilots, one team | strong team autonomy | central tool governance, many agent teams |

`centralized` is the default and what every module assumes.

**Federated trust is pure OAuth — there is no cross-account IAM on the data
plane.** The workload account's own credential provider holds the platform
Cognito M2M client id + secret; the token vault exchanges them at the platform
Cognito token endpoint; the platform gateway validates the resulting JWT against
its own issuer and cannot tell which account called it.

The **same `platform.yaml` deploys both sides** — the account you deploy into
decides the role. Platform side gets `auth`, `identity`, `gateway`,
`observability` (no runtimes); workload side gets `identity`, `memory`,
runtimes, `observability`. Deploying from an account in neither list fails at
synth with a message naming both.

An incomplete `deployment.federation` block raises a `ValueError` naming the four
required keys: `gateway_url`, `issuer_url`, `m2m_client_id`,
`m2m_client_secret_name`. Memory stays per-workload on purpose — conversation
history is the tenant boundary, and account isolation is the strongest wall
available.

---

## What is still billing after everyone goes home

The usual question at the end of day one is "what does this cost if we leave it up
overnight?" Answer it by meter, not by guess — most of the platform is pay-per-use
and genuinely near-zero idle, and the standing cost is short enough to enumerate.

**Standing hourly meters — these run whether or not anything is invoked.** Only
`enable_networking=true` creates any of them, which means `greenfield`, `migration`
and `multi-agent` leave **nothing** billing hourly, and `platform-team` and
`security-focused` do:

| Resource | Count | Meter |
|---|---|---|
| NAT gateway | **1** (`nat_gateways=1`) | per hour, plus per GB processed |
| Interface VPC endpoints | **5** × 2 AZs = **10** endpoint-hours | per endpoint per AZ per hour, plus per GB |
| S3 gateway endpoint | 1 | **free** |

The five interface endpoints are `bedrock-runtime`, `ecr.api`, `ecr.dkr`, `logs`,
and `bedrock-agentcore.gateway` (`stacks/networking_stack.py:70-105`), and the VPC
is `max_azs=2`, so each one is billed in two AZs. Ten endpoint-AZ-hours plus one
NAT-hour is the whole idle bill, and it is the reason the two networking profiles
carry a cost warning the other three do not.

There is **no flag that turns the endpoints off.** `enable_vpc_endpoints=True` is
passed as a literal at the call site (`app.py:197`), not read from context, so
`enable_networking=true` always means all five. Nor is there a flag for one AZ —
`max_azs=2` is hardcoded in the stack. If someone needs a cheaper networking
demo, the only levers are a source edit or not enabling networking at all; there
is no `-c` you can hand them. Say that plainly rather than hunting for a flag
during a session.

**Standing monthly meters — small but nonzero, and they are what makes "we forgot
about it" expensive over a quarter rather than a night:**

- **KMS CMK** with rotation enabled (`enable_kms`, `stacks/security_stack.py:15-19`) —
  per key per month.
- **Secrets Manager** entries created from IdP or API-key prompts — per secret per
  month. These are the ones people forget, because nothing in the stack list names
  them.
- **ECR image storage** — one arm64 image per agent pattern you built, so a
  pattern matrix leaves several. Bounded, though: a lifecycle rule keeps the last
  10 (`stacks/runtime_stack.py:107-109`), and the repository is
  `RemovalPolicy.DESTROY` with `empty_on_delete=True` (`:91-92`), so teardown does
  remove it. Unlike the log groups below, this one is not an orphan risk.
- **CloudWatch Logs storage** for retained log groups, including the orphans in
  "Tearing down" below.
- **S3** for the CloudTrail bucket.

**Zero idle cost, despite looking expensive:** AgentCore Runtime (per invocation),
Gateway (per request), Code Interpreter (per session), Cognito at workshop user
counts, CodeBuild (per build minute — a spike when you swap patterns, not a
standing charge).

Two account-scoped items behave differently from everything above and need naming
before you deploy, not in the wrap-up:

- **Transaction Search** (`enable_transaction_search`, defaults **true**) changes
  span-ingestion pricing **account-wide** and **survives teardown** by design.
- **CloudTrail.** The module E trail is management-events-only and single-region
  (`is_multi_region_trail=False`, no event selectors), so it is free — **unless the
  account already has a management-events trail**, in which case this is the second
  copy and every event bills. In a shared account, check first:

  ```bash
  aws cloudtrail describe-trails --query 'trailList[].[Name,IsMultiRegionTrail]' --output text
  ```

So: on `greenfield`, leaving it up overnight costs effectively nothing and the
honest answer is "nothing standing — tear it down tomorrow." On `platform-team`,
say "one NAT gateway and ten endpoint-hours per hour" and price those two lines in
the calculator for the customer's Region. Do not quote dollar figures from memory;
these rates vary by Region and change.

If the goal is a cheap platform left running between sessions, the lever is
`ENABLE_NETWORKING=false`, which removes every hourly meter at once.

---

## Tearing down

```bash
./scripts/deploy.sh destroy                  # everything, cascade intended
./scripts/deploy.sh destroy --stack <name>   # one stack — refuses if others depend on it
```

A targeted destroy passes `--exclusively`, so CloudFormation **refuses** rather
than cascading into stacks that depend on the target — which would take the
platform out from under other teams. The error names the consumer. That refusal
is the safety feature, not a bug.

### A destroy stops at the first stack that fails — check what is left

**This is the teardown fact that costs money, and it is the opposite of the
intuition.** A bare `destroy` runs `npx cdk destroy --all --force`
(`scripts/deploy.sh:590-595`), and CDK halts at the first stack it cannot delete.
Everything later in the sequence is never attempted.

The sequence is reverse-dependency order, so it goes observability → the runtime
stacks → gateway, memory, identity, auth, security → **networking last**. Which
means a failure in a *runtime* stack — early, cheap, and unrelated — leaves the
**NAT gateway and all five interface endpoints standing and billing hourly.**

Measured: a full-platform destroy ran 693s (11.6 min), failed on the fourth stack,
exited 1, and left six accelerator stacks up including `-networking`, with the NAT
gateway still `available`.

**So never treat `destroy` as fire-and-forget. Always confirm:**

```bash
./scripts/deploy.sh destroy; echo "destroy rc=$?"

# rc != 0 means stacks are still standing. Find them:
aws cloudformation list-stacks \
  --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" \
  --output text | sort

# And specifically: is anything still on an hourly meter?
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].NatGatewayId' --output text
```

Fix the failed stack, then **re-run `destroy`** — it picks up the remainder.

Two expected annoyances:

- The **networking stack can fail to delete for up to ~8 hours** while
  AgentCore's `agentic_ai` ENIs drain. If networking is genuinely the *only* thing
  left, NAT and the endpoints are already gone with it and the wait costs nothing.
  That reassurance only holds when the destroy got that far — see the abort
  behaviour above.
- **Transaction Search stays enabled.** Account-scoped, and other workloads may
  now depend on it. Revert deliberately:
  `aws xray update-trace-segment-destination --destination XRay`.

Then sweep, because `destroy` leaves things behind in every Region it touched.
All of it is cheap, all of it is confusing to find later, and none of it is a bug.

A verified sweep on a full platform-team teardown found **everything clean at the
service level** — no runtimes, gateways, memories, ECR repositories, SSM
parameters, Secrets Manager entries, Cognito pools or CodeBuild projects — and
**13 orphaned log groups**, in three classes. Service-created log groups are not
CloudFormation resources, so a clean stack delete does not touch them:

| Class | Prefix | Count in that run |
|---|---|---|
| Lambda | `/aws/lambda/<prefix>-…` | 7 |
| CodeBuild | `/aws/codebuild/<prefix>-build-<component>` | 3 |
| AgentCore runtimes | `/aws/bedrock-agentcore/runtimes/<runtime_name>-<id>-DEFAULT` | 3 |

**Two traps in sweeping them, and both will bite a one-liner:**

1. **The runtime log groups use underscores, not hyphens.** A runtime name cannot
   contain a hyphen, so `agentcore-workshop-dev` becomes
   `agentcore_workshop_dev_orchestrator`. A sweep keyed on `$PREFIX` finds the
   Lambda and CodeBuild groups and silently misses the AgentCore ones.
2. **`/aws/bedrock-agentcore/` is a shared namespace.** Any other AgentCore work in
   the account has log groups under the same prefix. Match on the project name, not
   on `/aws/bedrock-agentcore/`, or you will delete someone else's logs.

```bash
aws logs describe-log-groups --query "logGroups[?\
starts_with(logGroupName,'/aws/lambda/${PREFIX}')||\
starts_with(logGroupName,'/aws/codebuild/${PREFIX}')||\
starts_with(logGroupName,'/aws/bedrock-agentcore/runtimes/${PREFIX//-/_}')\
].logGroupName" --output text | tr '\t' '\n' > /tmp/orphans.txt

# Read the file before running this.
while IFS= read -r g; do
  [ -n "$g" ] && aws logs delete-log-group --log-group-name "$g"
done < /tmp/orphans.txt
```

Use the read loop rather than `for g in $(…)`: under zsh an unquoted variable does
not word-split, so the whole tab-separated list arrives as one name and the API
rejects it with `InvalidParameterException … must have length less than or equal
to 512`.

One more, and it is deliberately *not* in the sweep above:

```bash
# The CDK staging bucket, cdk-hnb659fds-assets-<account>-<region>. It is
# RETAIN by design, versioned, and survives deleting CDKToolkit itself —
# so DeleteBucket fails with BucketNotEmpty until you purge every version.
```

Leave the bucket alone unless you are cleaning up a Region you never meant to
deploy into; it is shared by every CDK app in the account and Region.

Capture the deployment before destroying it:

```bash
./scripts/deploy.sh export      # workshop-outputs-<stamp>.json
```
