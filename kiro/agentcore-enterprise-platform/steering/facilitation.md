# Running a guided session

Read this when you are facilitating a workshop, guided team build, or builder
session with this accelerator — planning the agenda, prepping the room, or
recovering in front of an audience.

**The single highest-leverage thing you can do is run `--dry-run` for the chosen
profile before anyone arrives, and again on screen as step one.** It prints every
module, the stacks it will deploy, and the exact verify command, and it makes
**zero AWS calls**. Everything below is downstream of that.

```bash
./scripts/deploy.sh workshop --dry-run --profile greenfield
```

Three things to know before you rely on it:

- **`--dry-run` belongs to `workshop`, not `deploy`.** `deploy --dry-run` accepts
  the flag and ignores it — it bootstraps and deploys for real. If you are
  previewing, the word `workshop` has to be in the command.
- **A misspelled flag is silently ignored.** `--dryrun` is not `--dry-run`; the
  parser drops what it does not recognise and the run is real. Confirm from the
  output, not from what you typed — a real dry run prints
  `DRY RUN — nothing will be deployed` and never reaches a
  `═══ Deploying ═══` header.
- **`security-focused` needs `ORG_ID` even to dry-run.** That gate runs before the
  plan is printed, so without it you get no plan: at a terminal it stops and
  prompts. Export a real id, or use any `o-…`-shaped value just to preview
  (`ORG_ID=o-preview0 …`), since dry-run makes no AWS calls.

---

## The day before

Do these yourself, in the actual account, on the actual laptop that will be
sharing a screen.

```bash
# 1. Local toolchain — this is where the room loses its first 20 minutes
python3.13 --version                      # exactly this name, not python3
bash --version                            # 4+; macOS /bin/bash is 3.2
node --version && npm --version
aws --version

# 2. Repo ready
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install -g aws-cdk@latest             # do this AFTER pip: the CLI must not be
cdk --version                             #   older than the aws-cdk-lib pip just installed
make lint && make test-controls           # no AWS needed

# 2b. macOS only — the verify scripts use bare urllib, not boto3, so a python.org
#     python3.13 fails every one of them with CERTIFICATE_VERIFY_FAILED
python3.13 -c "import ssl; print(ssl.get_default_verify_paths().cafile)"   # None == broken
"/Applications/Python 3.13/Install Certificates.command"                  # once per machine

# 3. Account ready — and it is the account you think it is
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN   # these beat AWS_PROFILE
aws sts get-caller-identity --query '[Account,Arn]' --output text
aws bedrock list-foundation-models --region $AWS_REGION \
  --query "modelSummaries[?contains(modelId,'anthropic')].modelId" --output text
aws bedrock-agentcore-control list-gateways --region $AWS_REGION >/dev/null \
  && echo "AgentCore responds in $AWS_REGION"   # the script never checks this

# 4. The plan you will actually walk
./scripts/deploy.sh workshop --dry-run --profile <p>
```

Then decide these four things in advance, because deciding them live costs the
room its momentum:

| Decision | Where it goes | Note |
|---|---|---|
| Profile | `--profile <p>` | see the picker in `deploy.md` |
| Region | `AWS_REGION` | `us-east-1` unless there is a reason; web search is Region-gated |
| Agent pattern | `AGENT_PATTERN` | `orchestrator` for a first pass; a framework the customer uses if the pitch is "framework-agnostic" |
| Model | `MODEL_ID` | override with a current cross-region inference profile rather than trusting in-code defaults |

**Do a full dress rehearsal in the same account and Region at least once,
including teardown.** The failures worth knowing about are account-specific: AZ
id mapping, Bedrock model enablement, Region-gated connectors, Organizations
membership.

### Prerequisites to state to participants in writing

| Tool | Required? | Note |
|---|---|---|
| `python3.13` | Yes | exactly this name on PATH |
| `node` + `npm` | Yes | the CDK CLI runs through npx |
| A **current** CDK CLI | Yes | `npm install -g aws-cdk@latest`. An old global CLI passes the prereq check and then fails bootstrap on a schema mismatch — the single most likely way to lose the first ten minutes |
| `aws` CLI | Yes | with working credentials |
| `bash` 4+ | Yes | macOS ships 3.2 — `brew install bash` |
| Docker / finch | **No** | images build in AWS CodeBuild |

Plus: **Bedrock model access enabled in the session Region before module 6**, and
an account where they can create IAM roles, Cognito pools, ECR repositories,
CodeBuild projects, and AgentCore resources. Sandbox or dev.

If the profile is `security-focused` — or anything with
`enable_resource_policies` — they also need an AWS Organizations id, and the
account must actually be in an Organization:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

---

## What the guided run does per module

`workshop` loops: **explain → deploy → verify → pause**. The narration is in the
script, so you are not improvising the "why" for each layer.

```bash
./scripts/deploy.sh workshop --profile <p>
./scripts/deploy.sh workshop --from 6            # resume where you stopped
```

Two mechanics to know before you rely on them:

- **The pause is a bare `read`** — it waits for ENTER after each module. Under
  `NON_INTERACTIVE=1` it does not pause at all, which is right for CI and wrong
  for a room.
- **A failed verify prompts `Continue anyway? [y/N]`** and defaults to aborting.
  Under `NON_INTERACTIVE=1` a failed verify **exits 1** with no prompt. So do not
  set `NON_INTERACTIVE=1` for a live walk.

`--from` is validated against the selected profile's sequence and fails with the
full sequence printed if the module is not in it. Skipped modules are logged as
skipped, not silently dropped.

---

## Timing an agenda

Deploy times from real runs. These are the floor — add prompts, discussion, and
the room's own questions.

| Profile | Sequence | Deploy time |
|---|---|---|
| `greenfield` | 3 4 5 6 9 | ~18 min |
| `migration` | 3 4 6 7 9 | ~18 min |
| `multi-agent` | 3 4 5 6 7 8 9 | ~29 min |
| `platform-team` | 3 4 5 A 6 7 8 9 C E | ~39 min |
| `security-focused` | 3 4 5 6 9 E | ~21 min |

Per module: 3 ≈ 2 min · 4 ≈ 2 min · 5 ≈ 3 min · A ≈ 2 min · **6 ≈ 7–8 min** ·
7 ≈ 3 min · **8 ≈ 8 min** · 9 ≈ 3 min · C ≈ 5 min · E ≈ 3 min · B ≈ a full
container rebuild · D ≈ discussion only, deploys nothing.

Realistic shape for a full day on `greenfield` or `migration`: about a third
deploying, a third verifying and reading what landed, a third discussion —
extension points, the customer's own tools, what production would need. If you
only have a half day, run `greenfield` and cut module 9's discussion, not its
deploy.

### Fill module 6's silent seven minutes deliberately

It is the one long gap and it happens early. Have something ready:

- Start the local dashboard before module 6 so the room has something to watch
  (status only, no AWS resources):

  ```bash
  .venv/bin/python dashboard/monitor.py &
  python3 -m http.server 8888 -d dashboard/public   # http://localhost:8888
  ```

- Or show the build actually moving:

  ```bash
  aws codebuild list-builds-for-project --project-name $PREFIX-build-orchestrator \
    --query 'ids[0]' --output text
  ```

- Or use the time for the architecture conversation the module is about: why the
  image is arm64, why the build runs in CodeBuild rather than on laptops, what
  the content-hash image tag means for CI.

**Say "this takes about seven minutes with no output" before you press enter, not
after minute four.** Unannounced silence reads as a broken demo; announced
silence reads as a container build.

---

## Splitting a room across workstreams

For a group large enough that watching one screen wastes people:

| Team | `--team` | Stacks |
|---|---|---|
| Platform | `platform` | `-networking -auth -identity -gateway -observability` |
| Agent | `agent` | `-runtime-orchestrator -runtime-code-agent -runtime-research-agent -memory` |
| Security | `security` | `-security -observability` |

```bash
./scripts/deploy.sh deploy --team agent
```

`-observability` is deliberately in two teams; CDK is idempotent so the overlap
is harmless.

**Two of the three teams fail out of the box, and this will happen live.**
`TEAM_MAP` names stacks that the app only creates behind a feature flag
(`scripts/deploy.sh:203-206`, `app.py:81-82`), and `cdk deploy` refuses the whole
batch when any one name does not exist — so nothing deploys, not even the four
stacks that were fine:

Which teams survive a bare `deploy --team` depends entirely on the manifest in
force, because `TEAM_MAP` names stacks the manifest may not declare. Computed
from `expected_stacks()` for each preset — pick the row matching the profile you
materialized:

| Manifest | `--team platform` | `--team agent` | `--team security` |
|---|---|---|---|
| `greenfield` | fails: `-networking` | fails: the two A2A runtimes | fails: `-security` |
| `migration` | fails: `-networking` | fails: the two A2A runtimes | fails: `-security` |
| `multi-agent` | fails: `-networking` | **works** | fails: `-security` |
| `platform-team` | **works** | **works** | **works** |
| `security-focused` | **works** | fails: the two A2A runtimes | **works** |

`platform-team` is the only manifest under which all three splits work bare, and
that is not a coincidence — it is the profile written for this exact scenario. If
the room is splitting by team, materialize `platform-team` first:

```bash
./scripts/deploy.sh deploy --profile platform-team --module 3
```

It turns networking and security on, which starts two hourly meters. That is a
cost decision to make in the open rather than a flag to paste in a hurry.

**A misspelled team no longer deploys everything.** This used to be the sharpest
edge in the script: `--team` values were never validated, `--profile` only on the
`workshop` action, and a bad value fell through the chain that sets `CDK_STACKS`,
leaving it empty — and an empty stack list means `cdk deploy --all`. Upstream
closed it. The parser now rejects unknown options, and `--profile`/`--team`
values are checked for every action (`scripts/deploy.sh:955-990`), so
`--team platfrom` and `deploy --profile greenfeild` exit 1 with the valid list
printed. Read the `Team … → Stacks:` line back anyway — it is still the fastest
way to see what you are about to approve.

Two constraints that decide whether this works:

- **Teams are not independent.** The agent team's runtimes need the platform
  team's Cognito issuer and gateway. Run module 3 and 4 together, for everyone,
  before splitting.
- **Give each team its own account, or its own `PROJECT_NAME`/`ENVIRONMENT`.**
  Sharing a prefix in one account means two `cdk deploy` runs fighting over the
  same stacks. Different prefixes in one account is fine and cheap:

  ```bash
  PROJECT_NAME=team-a ENVIRONMENT=dev ./scripts/deploy.sh deploy --team agent
  ```

Reconvene on a shared verify — `./scripts/deploy.sh verify` in each team's
`PROJECT_NAME`, then `invoke.py` — so the room sees one working platform rather
than three partial ones. `verify` derives each team's footprint from its own
config, which is what makes it usable across a split.

---

## The five things to pre-empt

Every one of these is a real question that has cost a session time. Say them
before they happen; they land as expertise and afterwards as excuses.

1. **`--profile` on its own deploys everything the manifest declares**, not just
   that profile's modules, because it materializes the preset and then runs
   `cdk deploy --all`. Use `workshop --profile` for the guided walk, or combine
   `--profile … --module …`. Measured at 6 stacks for `greenfield`, 10 for
   `platform-team` (`deploy.md` has all five). Interactively you now get a
   `Proceed to deploy ALL of the above? [y/N]` prompt with the full stack list —
   read it out to the room; that is a better teaching moment than the warning.
   Under `--yes` or `NON_INTERACTIVE=1` there is no prompt. Two related edges are
   **fixed** upstream and no longer worth pre-empting — misspelled flags and
   misspelled `--profile`/`--team` values now exit 1 instead of deploying the lot.
   What survives is `deploy --dry-run`, spelled correctly: the `deploy` action
   accepts it and ignores it.
2. **Module 6 is silent for ~7–8 minutes.** Remote arm64 container build.
3. **The default `orchestrator` agent has no tools and reads no caller
   identity.** Both deliberate. Asking it "what tools do you have?" correctly
   returns nothing — tools live on the gateway (`invoke.py --tools`). If someone
   is going to ask a tool question, deploy `strands-agent` or `langgraph-agent`.
4. **`CREATE_COMPLETE` proves nothing about behaviour.** Say it once, early, and
   then run every verify. It is also the honest framing for the security
   conversation: module E's verify is a stack-status check because there is no
   behavioural probe for "is this control enforcing."
5. **Module 4 deploys less than its title suggests.** The gateway M2M credential
   provider is always created; enterprise IdP federation only happens if a
   federated IdP was chosen, and the 3LO providers only when client ids are
   supplied. Nothing silently half-configures — but say so, or someone concludes
   it is broken.

---

## Before you share your screen: a deploy prints a live secret

**Once module 3 has run in this account, every later `deploy.sh deploy` prints the
Cognito M2M app client secret in plaintext** — under a CDK-generated key ending
`…UserPoolClientClientSecret…`. It scrolls past in a wall of ids and looks like
every other one of them.

**Every run. Not "modules 3 and 4", and not even "runs that touch `-auth`."** The
script's end-of-run summary does its own account query for every stack whose name
starts with the project prefix and dumps all of their outputs, regardless of what
you deployed. Verified with `deploy --module C`: CDK's own `Outputs:` block listed
only the networking stack, and the summary table printed the `-auth` secret anyway.
Resumed runs (`--from 6`) walk it too. There is no flag that turns this off.
`security.md` has the mechanism and the mitigations.

Three things that cost nothing:

- **Do not assume a narrowly scoped `--module` is safe to screen-share.** No
  `deploy` invocation is, once `-auth` exists. Scroll the summary table off screen
  before you stop presenting, or run deploys off-screen and screen-share the
  verifies.
- **If it does go on screen, say so and rotate afterwards** rather than hoping
  nobody scrolled back. In a throwaway workshop account, teardown is the rotation.
- **Do not paste raw deploy output into a ticket or chat** when asking for help.
  The collection commands in `verify.md` are safe; a full deploy log is not.

Worth saying out loud when it happens, because a security-minded participant will
spot it and the honest version is a better moment than the caught-out one: this is
the finding to raise in the review, and it is one read-only API call to confirm.

---

## When something breaks in front of people

This is the part that decides how the session is remembered. The accelerator's
failure messages are good; use them rather than improvising.

1. **Read the actual error out loud.** The verify prints why it failed.
2. **Container failures name their own cause in the logs.** Every runtime failure
   so far has:

   ```bash
   ARN=$(aws ssm get-parameter --name /$PROJECT_NAME/$ENVIRONMENT/runtimes/orchestrator/arn \
     --query Parameter.Value --output text)
   aws logs filter-log-events \
     --log-group-name "/aws/bedrock-agentcore/runtimes/${ARN##*/}-DEFAULT" \
     --start-time $(( ($(date +%s) - 900) * 1000 )) \
     --query 'events[].message' --output text | grep -iE 'error|denied|traceback'
   ```

3. **Answer `y` to "Continue anyway?" and write down which module failed.**
   Later modules build on it, and the failure usually explains a stranger symptom
   two modules later. Debugging live at minute 40 costs the rest of the agenda.
4. **A stack that died mid-deploy is safe to re-run.** CDK picks up from current
   state, and `UPDATE_ROLLBACK_COMPLETE` can be deployed onto again.
5. **If you hit Ctrl-C — because the room is waiting and you panicked — nothing
   in AWS stopped.** The script dies instantly and prints
   `[ERROR] Failed to deploy <stack>`, which looks exactly like a real failure and
   is not one. Measured: the stack went on to `UPDATE_COMPLETE` by itself, the
   CodeBuild build succeeded, the runtime came back `READY`, and a re-run reported
   `(no changes)` in 27.6s. Check `describe-stacks` before you tell the room
   anything failed, and if it says `UPDATE_IN_PROGRESS`, wait rather than retry.
6. **Then go to `troubleshooting.md`**, which is organised by what the person
   sees rather than by what the code does.

Treat a real failure as content, not as a setback: "this is the failure mode you
will hit in your own account, and here is how the platform tells you" is more
valuable than a clean run.

---

## Cost, and the three settings that are not pay-per-use

Most of the platform is pay-per-use and nearly free idle. Three exceptions to say
out loud **before** deploying, not in the wrap-up:

| Item | Why it matters |
|---|---|
| **NAT gateway + VPC interface endpoints** (`enable_networking`) | bill **hourly**, whether or not anything runs — 1 NAT plus 5 endpoints × 2 AZs. `platform-team` and `security-focused` both turn networking on; the other three profiles leave nothing hourly. |
| **Transaction Search** (`enable_transaction_search`, defaults **true**) | changes span-ingestion pricing **account-wide** and **survives teardown** by design, because other workloads may come to depend on it. |
| **CodeBuild** | per-build minutes; every pattern swap is another arm64 build. |

**Someone will ask "what does this cost if we leave it up overnight?" — usually at
the end of day one, in front of whoever owns the account.** Have the answer ready
rather than promising to follow up. On `greenfield`, `migration` or `multi-agent`
the honest answer is "nothing standing"; on the two networking profiles it is "one
NAT gateway and ten endpoint-AZ-hours per hour, plus a KMS key and any Secrets
Manager entries per month." `deploy.md` has the itemised inventory. Give the meters
and offer to price them in the calculator for their Region — do not quote dollar
rates from memory.

If the session is in an account where an account-wide setting is not yours to
change, decide before module 9:

```bash
ENABLE_TRANSACTION_SEARCH=false ./scripts/deploy.sh deploy --module 9
```

…and then say plainly that tracing will not work — every OTLP span batch is
rejected with HTTP 400 while the deploy still reports success. That tradeoff is
better than surprising the account owner.

Revert it deliberately after a session if you need to:

```bash
aws xray update-trace-segment-destination --destination XRay
```

---

## Capture before you tear down

The export is the artifact participants take home — every SSM parameter and stack
output in one file, which is also the input to any follow-on work.

```bash
./scripts/deploy.sh export        # → workshop-outputs-<stamp>.json
```

**This file contains account ids and resource ARNs.** Do not commit it or paste
it into a shared channel unaltered.

---

## Teardown, same day

```bash
./scripts/deploy.sh destroy; echo "destroy rc=$?"
```

**Check that exit code before you close the laptop.** A destroy stops at the first
stack it cannot delete, and because networking is torn down *last*, an unrelated
failure in a cheap runtime stack leaves the NAT gateway billing overnight. Measured
on a real run: exit 1 after 11.6 minutes, failed on the fourth of ten stacks, six
stacks still up. The cause was a runtime `DELETE_FAILED` / `NotStabilized`, which is
a stabilization timeout rather than a real failure — retrying that one stack cleared
it in 34 seconds, and a second `destroy` took out the rest. `troubleshooting.md` has
both entries.

This is the one teardown step worth assigning to a named person with a calendar
reminder, not a "someone will check tomorrow."

Then sweep, because two things linger by design:

```bash
# Anything left standing
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'$PREFIX')].{n:StackName,s:StackStatus}" --output table

# Anything still on an hourly meter
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].NatGatewayId' --output text

# ENIs that block subnet/security-group deletion for up to ~8 hours
aws ec2 describe-network-interfaces \
  --filters Name=interface-type,Values=agentic_ai \
  --query 'NetworkInterfaces[].{id:NetworkInterfaceId,status:Status,subnet:SubnetId}'
```

- The **networking stack can fail to delete for up to ~8 hours** while AgentCore's
  `agentic_ai` ENIs drain. NAT and endpoints are already gone by then, so the wait
  costs nothing meaningful — but somebody has to come back and finish it. Own
  that, or hand it to a named person.
- **Transaction Search stays enabled.** Account-scoped, on purpose.

If you ran a pattern matrix or several profiles, also check the resources CDK does
not always take with it: ECR repositories, CloudWatch log groups, and any Secrets
Manager entries created from API-key prompts.

---

## A ready-made run of show — `greenfield`, one day

| Slot | What | Notes |
|---|---|---|
| Open | `workshop --dry-run` on screen | the whole plan, zero AWS calls; sets expectations for the day |
| Module 3 | Cognito, OAuth clients, SSM registry | frame it as the trust root: every AgentCore call here is authenticated |
| Module 4 | the M2M credential provider | say what it does *not* deploy by default |
| Module 5 | gateway + Lambda tool target | `test_gateway.py` is a real `tools/list` + `tools/call` |
| Module 6 | the agent | announce the 7–8 minutes; run the dashboard; talk architecture |
| Verify | `deploy.sh verify`, then `invoke.py --tools` | one command covers the footprint; make the "registered vs loaded" distinction explicit |
| Module 9 | observability | disclose the account-wide Transaction Search setting *before* deploying |
| Extend | add a tool target (module 7 material) | the moment it stops being a demo: agents pick up new tools with **no agent redeploy** |
| Swap | `AGENT_PATTERN=<their framework>` on module 6 | the framework-agnostic claim, demonstrated rather than asserted |
| Close | `export`, then `destroy` | hand over the outputs file; name who checks the networking stack tomorrow |

The two slots that produce the strongest reaction are **Extend** and **Swap** —
both are cheap, both are the actual differentiators, and both are the first things
cut when the morning runs long. Protect them by starting teardown on time, not by
shortening them.
