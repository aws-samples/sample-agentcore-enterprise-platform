# Deploy one module

Read this when someone wants a single module rather than a whole profile walk -
adding memory, redoing a failed layer, swapping the agent pattern, or turning on
networking or security.

**This is a runbook, not reference material.** Follow it in order. Anything that
creates, changes, deletes or bills goes one command at a time: state what it
does and what it costs, then stop and wait for approval. Never group one of
those with anything else. The halt conditions at the end are not advisory.

The standalone `deploy --module` path is what people use to add a layer or redo a
broken one. It has three sharp edges the guided walk hides, and all three fail in
ways that look like something else.

## The rule

**Propose one command, state what it creates, its cost posture and its expected
duration, then stop and wait.** Read-only checks are cheap approvals — use them
freely instead of guessing.

**Say this before the first `deploy`:** once the `-auth` stack exists, **every**
`deploy` prints the Cognito M2M client secret to stdout in plaintext, including
runs that never touch `-auth`. No `deploy` is screen-safe.

**Never propose `deploy --dry-run`.** The flag is parsed and ignored on this
action; it bootstraps and deploys for real. To preview, use
`./scripts/deploy.sh workshop --dry-run` or `synth`.

## Step 1 — check the dependencies exist (read-only)

A module deployed onto a missing layer fails deep in CloudFormation with an error
about the wrong thing. Confirm the prerequisites first:

| Want | Needs already deployed | Why |
|---|---|---|
| 3 | — | trust root |
| 4 | 3 | needs the Cognito M2M client |
| 5 | 3, 4 | gateway needs JWT auth + the credential provider |
| A | 3 | memory is independent of the gateway |
| 6 | 3, 4, 5 (+ A if using memory) | the runtime resolves gateway + identity from SSM |
| 7 | 5 | grows the same `-gateway` stack |
| 8 | 3, 4, 5, 6 | sub-agents reach the gateway the same way |
| 9 | 6 | nothing to observe without a runtime |
| B | 6 | redeploys the orchestrator |
| C | — | standalone VPC, but see the ordering trap below |
| E | — | standalone KMS + CloudTrail |

```bash
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'agentcore-workshop-dev')].[StackName,StackStatus]" \
  --output table
```

Read the statuses, not just the names. A stack in `UPDATE_ROLLBACK_COMPLETE` is
safe to deploy onto again. A stack in `ROLLBACK_COMPLETE` was never successfully
created and must be deleted before it can be recreated.

## Step 2 — export the flag if this module lives behind one

Only module 8 turns its own flag on. This is the single most common failure on
this path:

| Module | Flag | App default | Bare `deploy --module` |
|---|---|---|---|
| 8 | `enable_a2a` | **`true`** | works |
| C | `enable_networking` | `false` | **fails**: `No stacks match the name(s) …-networking` |
| E | `enable_security` | `false` | **fails**: `No stacks match the name(s) …-security` |

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module C
ENABLE_SECURITY=true   ./scripts/deploy.sh deploy --module E
```

The error's advice to "check CloudFormation console for details" is a dead end —
the stack was never synthesized, so there is nothing in the console to look at.

## Step 3 — deploy

```bash
./scripts/deploy.sh deploy --module <N>
```

Never substitute `--profile` for `--module`. `--profile` materializes that
profile's preset as `platform.yaml` and then runs `cdk deploy --all` — the whole
manifest, not the profile's modules. To see the blast radius first without
writing anything, read `expected_stacks()` off the preset (`deploy.md`);
`ls --profile <p>` also rewrites `platform.yaml`.

Typos used to be the sharpest edge here — a misspelled flag or value was
discarded, which left the stack list empty, and empty means `cdk deploy --all`.
Upstream made the parser fail closed (`scripts/deploy.sh:955-990`): unknown
options exit 1 with the valid list, and `--profile`/`--team` values are validated
for every action, not just `workshop`. `--stack` with no value is caught too.
Do not spend the room's attention pre-empting this any more.

What still needs care, because neither is a spelling mistake:

- **`--dry-run` on `deploy`** is accepted and ignored — only `workshop` honours
  it. `deploy --module 3 --dry-run` bootstraps and deploys for real.
- **`--profile` with no other scope** is a legitimate command that means the whole
  manifest. Interactively `confirm_footprint` will show you the list and ask; with
  `--yes` or `NON_INTERACTIVE=1` it will not.

The tell is the echoed scope line. `Workshop Module 6 → Stacks: …` or
`Team agent → Stacks: …` means the flag took. No such line means the run is
unscoped, and the next thing to happen is a full deploy.

Modules with no stacks: `--module D` prints a pointer to `.gitlab-ci.yml` and
exits 0. `--module B` redeploys the orchestrator with the code interpreter and has
no verify command.

## Step 4 — verify

| Module | Verify |
|---|---|
| 3 | `aws ssm get-parameter --name /agentcore-workshop/dev/auth/issuer-url` |
| 4 | `aws ssm get-parameter --name /agentcore-workshop/dev/identity/gateway-credential-provider-name` |
| 5, 7 | `.venv/bin/python scripts/test_gateway.py` — then `invoke.py --tools` for 7 |
| A | `.venv/bin/python scripts/test_memory.py` |
| 6 | `.venv/bin/python scripts/invoke.py "Reply with exactly: WORKSHOP OK"` |
| 8 | `.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"` |
| 9 | `.venv/bin/python scripts/check_observability.py` |
| C | `.venv/bin/python scripts/check_network.py` |
| E | stack `CREATE_COMPLETE` only — there is no behavioural verify |

Two verifies mean less than they appear to, and saying so is the honest move:

- **`test_memory.py` uses your local credentials**, so it passes regardless of
  what the runtime role can actually do. It is not an isolation proof.
- **A `-security` stack reaching `CREATE_COMPLETE`** means the KMS key and trail
  exist, not that anything is enforced. Cedar ships `LOG_ONLY` with an
  unconstrained permit; the egress filter masks rather than blocks.

## The two ordering traps

**Module C after module 6 leaves the agent outside the VPC.** A runtime deployed
before networking keeps `networkMode: PUBLIC` until it is redeployed. If someone
adds networking to a running platform, module C alone is not enough — redeploy the
runtimes afterward:

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module 6
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module 8
```

Then `check_network.py`. Without the redeploy it reports the agent is not in the
VPC and it is correct.

**Module A after module 6 reports "no changes".** The orchestrator runtime
depends on the memory stack, so deploying 6 first creates memory implicitly.
That is why `platform-team` runs A before 6. Not a failure — just say what
happened rather than letting it read as a broken deploy.

## Swapping the agent pattern

Framework choice is decoupled from infrastructure — this is a module 6 redeploy,
no infra change:

```bash
AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6
```

Valid values are rejected up front with the list printed, so a typo fails fast
rather than deploying the default. Each swap is another arm64 CodeBuild build, so
budget ~7–8 minutes and mention the build cost.

Use `strands-agent` or `langgraph-agent` when the demo needs tools or memory —
the default `orchestrator` has neither. For recall *across* sessions also set
`USE_LONG_TERM_MEMORY=true`, which defaults to `false`; without it a new
`--session` answers `NO RECORD`.

## Halt conditions

- The dependency check shows a required stack missing or in `ROLLBACK_COMPLETE`.
- The module needs a flag and the participant has not agreed to enable it —
  `ENABLE_NETWORKING=true` starts an hourly meter, so that is a cost decision.
- The verify fails twice for the same reason. Route to
  `steering/troubleshooting.md` by symptom rather than retrying.
