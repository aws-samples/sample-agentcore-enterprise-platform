# Troubleshooting

Read this when something failed. Organised by **what the person sees**, not by
what the code does.

**Two rules that save the most time:**

1. **Read the container logs before changing anything.** Every runtime failure so
   far named its own cause there.

   ```bash
   ARN=$(aws ssm get-parameter --name /$PROJECT_NAME/$ENVIRONMENT/runtimes/orchestrator/arn \
     --query Parameter.Value --output text)
   aws logs filter-log-events \
     --log-group-name "/aws/bedrock-agentcore/runtimes/${ARN##*/}-DEFAULT" \
     --start-time $(( ($(date +%s) - 900) * 1000 )) \
     --query 'events[].message' --output text | grep -iE 'error|denied|traceback'
   ```

2. **`CREATE_COMPLETE` proves nothing about behaviour.** Run the verify script.

---

## Index

| What you see | Section |
|---|---|
| `This script requires bash 4 or newer` | [macOS bash 3.2](#this-script-requires-bash-4-or-newer-or-declare--a-invalid-option) |
| Script freezes after the docker check | [npx hang](#the-script-freezes-after-the-docker-check) |
| `python3.13: NOT FOUND` | [python3.13](#python313-not-found) |
| `CERTIFICATE_VERIFY_FAILED` from a verify script | [macOS Python certs](#certificate_verify_failed-from-a-verify-script-while-aws-commands-work-fine) |
| `AWS credentials invalid or expired` | [credentials — or a disabled Region](#aws-credentials-invalid-or-expired) |
| Commands hit the wrong account | [env vars beat profile](#commands-are-hitting-the-wrong-account) |
| `CDK bootstrap failed` / schema version mismatch | [CDK CLI too old](#cdk-bootstrap-failed--and-the-advice-printed-under-it-is-probably-wrong) |
| `No stacks match the name(s) …-security` / `…-networking` | [module needs its flag](#no-stacks-match-the-names-prefix-networking-or--security) |
| Deploy failed, CDK output unhelpful | [CloudFormation events](#a-stack-failed-and-the-cdk-output-is-not-enough) |
| Model not available / access denied | [Bedrock models](#bedrock-says-the-model-is-not-available-or-access-is-denied) |
| Deploy stops asking for an org id | [ORG_ID](#the-deploy-stops-asking-for-an-organizations-id) |
| `subnets are in unsupported availability zones` | [AZ ids, and the fix is a source edit](#the-following-subnets-are-in-unsupported-availability-zones-in-region-) |
| `CIDR … conflicts with another subnet` after an AZ change | [destroy before redeploying](#the-cidr-1002024-conflicts-with-another-subnet-after-changing-azs) |
| `networkMode is PUBLIC, not VPC` | [module C does not move runtimes](#check_networkpy-networkmode-is-public-not-vpc--the-vpc-exists-but-the-agent-is-not-in-it) |
| `workshop-outputs-*.json` stack outputs are garbage | [export merge is broken](#workshop-outputs-stampjson-has-no-usable-stack-outputs) |
| Changed agent code, redeploy did nothing | [image tags](#i-changed-agent-code-and-the-redeploy-changed-nothing) |
| `invoke.py` prints a wall of `data: {…}` JSON | [streaming pattern, not a crash](#invokepy-printed-hundreds-of-data--lines-instead-of-an-answer) |
| Memory-using pattern forgets across sessions | [long-term memory is off by default](#a-memory-using-pattern-recalls-within-a-session-but-not-across-sessions)  |
| `platform.yaml is invalid` | [config validation](#platformyaml-is-invalid) |
| Stale answers keep coming back | [workshop.env](#old-answers-keep-coming-back) |
| Module 6 has been silent for minutes | [not a hang](#module-6-has-been-silent-for-eight-minutes) |
| I pressed Ctrl-C mid-deploy | [AWS keeps going](#i-pressed-ctrl-c-during-a-deploy) |
| `--dry-run` deployed for real | [wrong action, or misspelled flag](#i-passed---dry-run-and-it-deployed-anyway) |
| I deployed way more than I expected | [`--profile` scope](#i-deployed-far-more-than-i-expected) |
| Invoke returns `Unauthorized` | [JWT](#invoke-returns-unauthorized) |
| Invoke returns HTTP 424 | [container serves the wrong protocol](#invoke-returns-http-424) |
| A2A invoke returns 200, no answer | [wrong JSON-RPC envelope](#an-a2a-invoke-returns-http-200-and-the-agent-never-answered) |
| `Invalid length for parameter runtimeSessionId` | [33-char minimum](#invalid-length-for-parameter-runtimesessionid) |
| `Authorization method mismatch` | [SigV4 vs JWT](#authorization-method-mismatch-on-invoke) |
| Agent reports no tools | [gateway tools](#the-agent-reports-no-tools) |
| Memory-backed pattern fails at invoke | [runtime role](#a-memory-backed-pattern-fails-at-invoke) |
| Module 9 verify: `status is PENDING` | [Transaction Search is async](#module-9s-verify-fails-with-trace-segment-destination-is-cloudwatchlogs-but-status-is-pending) |
| No traces anywhere | [Transaction Search](#no-traces-appear-anywhere) |
| `batch-get-traces` returns nothing | [1% sampling](#batch-get-traces-returns-nothing-for-my-trace) |
| Destroy fails on an export | [teardown order](#destroy---stack-fails-with-export--cannot-be-deleted) |
| `destroy` exited 1, stacks still up | [it aborts at the first failure, and networking is last](#destroy-exited-non-zero-and-half-the-platform-is-still-standing) |
| `DELETE_FAILED` / `NotStabilized` on a runtime | [a timeout, not a failure — retry](#delete_failed-request-timed-out-while-deleting-awsbedrockagentcoreruntime) |
| Networking destroy fails on subnets | [ENI drain](#destroying-the-networking-stack-fails-on-subnets-or-security-groups) |
| Deleted observability, Transaction Search still on | [account-scoped](#deleting-the-observability-stack-did-not-disable-transaction-search) |

---

## Local setup

### `This script requires bash 4 or newer` (or `declare: -A: invalid option`)

macOS ships bash 3.2 (2007) as `/bin/bash`; the script needs associative arrays.
The guard is the **first thing in the file** (`scripts/deploy.sh:5`, on
`BASH_VERSINFO`), so it fires before any subcommand runs — `ls`, `config` and
`workshop` all stop identically, and it exits 1. Its own advice is correct and
complete:

```
ERROR: This script requires bash 4 or newer (you are running bash 3.2.57(1)-release).

macOS ships bash 3.2 as /bin/bash. To fix:
  1. brew install bash
  2. Run the script with the new bash explicitly:
       bash scripts/deploy.sh deploy
```

```bash
brew install bash
bash scripts/deploy.sh deploy        # explicit new bash
# or: hash -r    so PATH picks up /opt/homebrew/bin/bash
```

You will only see the raw `declare: -A: invalid option` if the guard was removed or
you are sourcing pieces of the script by hand. **Do not go looking for that string** —
on a stock checkout the clean message above is what appears.

### The script freezes after the docker check

An old checkout. Older copies probed the CDK CLI with a bare `npx cdk --version`;
when `aws-cdk` is not in the npx cache, npx asks "Ok to proceed?" and — with
output suppressed and no TTY — waits forever. The fix (`npx --no-install cdk
--version`) is already in `check_prereqs`. Meanwhile:

```bash
npm install -g aws-cdk
```

### `python3.13 NOT FOUND`

The scripts require exactly `python3.13` on PATH, not `python3`. Install it, then
rebuild the venv:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### `CERTIFICATE_VERIFY_FAILED` from a verify script, while `aws` commands work fine

```
ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify
failed: unable to get local issuer certificate (_ssl.c:1028)
```

Seen from `test_gateway.py`, `invoke.py` or `deploy.sh verify` — typically at
`utils.py` → `get_m2m_token`, the step that fetches a Cognito token. **The stack is
fine. The gateway is fine.** Nothing is wrong in AWS.

The tell is the asymmetry: every `aws` CLI call and every boto3 call in the same run
succeeds, and only the accelerator's own Python scripts fail. That is because
botocore ships its own CA bundle (`certifi`), while these scripts call
`urllib.request.urlopen` with no `context=` argument and so fall back to OpenSSL's
default path — and on a **python.org macOS installer** build of `python3.13` that
path does not exist:

```bash
python3.13 -c "import ssl; print(ssl.get_default_verify_paths().cafile)"   # None
```

Two fixes. The official one, run once per machine:

```bash
"/Applications/Python 3.13/Install Certificates.command"
```

Or, where a participant cannot or would rather not run that, a per-shell override
that needs no privileges:

```bash
export SSL_CERT_FILE=$(.venv/bin/python -m certifi)
```

Either makes the whole verify layer work — one env var turned a failing
`test_gateway.py` into `tools/list` returning both targets and a successful
`tools/call`.

Affects `python3.13` from the python.org installer specifically. Homebrew and
`uv`-managed interpreters link a real trust store and never see this.

**Why it can end a run:** module 5's stack reaches `CREATE_COMPLETE`, then its
verify fails. Under `NON_INTERACTIVE=1` the script aborts the whole guided run
(`Aborting (NON_INTERACTIVE=1 cannot prompt)`); at a terminal it offers to
continue. Fix the certs and resume with `--from`, do not skip the verify.

### Do I need Docker?

**No.** Images build in AWS CodeBuild. `check_prereqs` reports docker as optional
and continues. You only need a container runtime to build and run an agent image
locally while developing.

---

## Credentials and permissions

### `AWS credentials invalid or expired`

Refresh and retry. Long deploys can outlive a session, so this can also appear
*mid-run*:

```bash
aws sso login            # or your credential process
aws sts get-caller-identity
```

If a deploy died mid-stack, re-run the same command — CDK picks up from current
stack state, and `UPDATE_ROLLBACK_COMPLETE` is safe to deploy onto again.

**But check the Region before you touch your credentials** — the message is
misattributed. `check_credentials` (`scripts/deploy.sh:291-297`) tests
`aws sts get-caller-identity &>/dev/null` and prints this on *any* non-zero exit,
having discarded the actual error. Measured against an opt-in Region that is not
enabled for the account:

```
[ERROR] AWS credentials invalid or expired.
[ERROR] Run 'aws sso login' or configure credentials, then retry.
```

The credentials were valid. The real error, visible only when you run the call
yourself:

```bash
aws sts get-caller-identity --region eu-central-2
# An error occurred (InvalidClientTokenId) … The security token included in the
# request is invalid.        ← STS in a disabled opt-in Region
aws sts get-caller-identity --region us-east-11
# Could not connect to the endpoint URL: "https://sts.us-east-11.amazonaws.com/"
#                           ← a typo'd Region name
```

So `aws sso login` can never fix it. Run `get-caller-identity` yourself against
`$AWS_REGION`, and enable the Region (Account settings → Regions) or fix the
spelling. Region-enablement takes minutes and is account-wide.

### Commands are hitting the wrong account

`AWS_ACCESS_KEY_ID` / `AWS_SESSION_TOKEN` in the environment beat `AWS_PROFILE`.
That is almost always the cause.

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sts get-caller-identity --query Account --output text
```

Also note: an **empty** `AWS_PROFILE=""` is worse than unset — the CLI reports
`The config profile () could not be found`. Unset it, do not blank it.

### CDK bootstrap failed — and the advice printed under it is probably wrong

The script prints this on any bootstrap failure:

```
[ERROR] CDK bootstrap failed for aws://<account>/<region>. Output:
...
[ERROR] Common causes: wrong AWS account/profile, or missing IAM permissions
[ERROR] to create the CDK bootstrap stack (CDKToolkit). Fix and retry.
```

**Read the CDK output above those two lines before believing them.** On a fresh
clone the most common cause is neither of the ones named — it is a CDK CLI that is
too old for the `aws-cdk-lib` pip just installed:

```
This CDK CLI is not compatible with the CDK library used by your application.
(Cloud assembly schema version mismatch: Maximum schema version supported is
53.x.x, but found 54.0.0. You need at least CLI version 2.1138.0 to read this
manifest.)
```

Fix the **CLI**, not the library:

```bash
npm install -g aws-cdk@latest
cdk --version                        # must be >= the version the error named
```

Why it happens, and why it will keep happening: `requirements.txt` pins
`aws-cdk-lib>=2.265.0` with no upper bound, so `pip install -r requirements.txt`
always pulls the newest library, which emits the newest cloud-assembly schema. The
repo has no `package.json` and no `node_modules`, so `npx --no-install cdk`
resolves whatever CDK CLI is installed **globally** — often one installed months
ago and never touched. The library moves on every clone; the CLI does not.

`check_prereqs` will not catch it. It prints a green `✓ cdk: 2.1119.0` because it
tests that the CLI *exists*, never that it is compatible with the installed
library. So the run looks healthy right up to the bootstrap step, then dies in
about ten seconds.

Do **not** "fix" this by pinning `aws-cdk-lib` down to match an old CLI. The
version the error message names is the floor; install it or newer.

The two causes the script *does* name are real, just rarer here. Once the CLI is
current and it still fails, then confirm the account and the IAM permission to
create `CDKToolkit`:

```bash
aws sts get-caller-identity --query '[Account,Arn]' --output text
aws cloudformation describe-stacks --stack-name CDKToolkit \
  --query 'Stacks[0].[StackStatus,LastUpdatedTime]' --output text
```

---

## Deploy failures

### `No stacks match the name(s) <prefix>-networking` (or `-security`)

```
No stacks match the name(s) agentcore-workshop-dev-security
[ERROR] Failed to deploy agentcore-workshop-dev-security
[ERROR] Check CloudFormation console for details.
```

**Ignore the advice — there is nothing in the CloudFormation console to look at.**
The stack was never synthesized, so CloudFormation has never heard of it. The script
exits 1, correctly, but points you at the wrong place.

Cause: `deploy --module C` and `deploy --module E` name a stack that only exists in
the CDK app when a feature flag is on, and **neither module turns its own flag on.**
`app.py` defaults `enable_networking=false` and `enable_security=false`, so a bare
standalone deploy of either asks CDK for a stack that is not in the app. Export the
flag:

```bash
ENABLE_NETWORKING=true ./scripts/deploy.sh deploy --module C
ENABLE_SECURITY=true   ./scripts/deploy.sh deploy --module E
```

**Module 8 is the exception, and it is the reason this surprises people.**
`enable_a2a` defaults **`true`** at app level (`app.py:83`), so `deploy --module 8`
just works — and the guided loop additionally re-exports `ENABLE_A2A=true` at
`scripts/deploy.sh:901` so that a profile which set it false (`greenfield`,
`migration`, `security-focused`) does not break module 8 mid-walk. There is no
equivalent line for C or E. So "module 8 needed no flag" is true and does not
generalise.

Inside `workshop --profile …` none of this bites: `platform-team` and
`security-focused` already set the flags they need. The trap is only the standalone
`deploy --module` path — which is exactly what people use to redo one module.

Confirm what the app currently defines before blaming the deploy:

```bash
./scripts/deploy.sh ls                                   # stacks with current flags
ENABLE_SECURITY=true ./scripts/deploy.sh ls | grep security
```

### A stack failed and the CDK output is not enough

Ask CloudFormation directly — the resource-level reason is what you want:

```bash
aws cloudformation describe-stack-events --stack-name <stack> \
  --query "StackEvents[?contains(ResourceStatus,'FAILED')].{r:LogicalResourceId,reason:ResourceStatusReason}" \
  --output json | head -30
```

### Bedrock says the model is not available, or access is denied

Two independent causes, and they need different fixes:

- **Model access not enabled** in this account/Region → enable it in the Bedrock
  console (Model access), then retry.
- **The model id has aged out.** Dated model ids get marked Legacy and are
  rejected in fresh accounts. Every pattern takes an override rather than
  hardcoding:

  ```bash
  export MODEL_ID=us.anthropic.claude-sonnet-5      # or your current inference profile
  ./scripts/deploy.sh deploy --stack $PREFIX-runtime-orchestrator
  ```

Prefer a cross-region inference profile id over a dated model id.

**Match the exception to the cause — three distinct signatures, all measured
directly against `bedrock-runtime converse`, which is the fastest way to test a
model id without redeploying anything:**

```bash
aws bedrock-runtime converse --model-id "$MODEL_ID" \
  --messages '[{"role":"user","content":[{"text":"say OK"}]}]' \
  --inference-config '{"maxTokens":10}'
```

| Exception | Message | What it means |
|---|---|---|
| `ResourceNotFoundException` | `Access denied. This Model is marked by provider as Legacy and you have not been actively using the model in the last 30 days.` | the id aged out — **this is what a hardcoded model default eventually becomes**, and the wording says "Access denied" even though access is not the problem |
| `ValidationException` | `Invocation of model ID … with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile` | you dropped the `us.` prefix on a model that is inference-profile-only |
| `ValidationException` | `The provided model identifier is invalid.` | the id does not exist at all — a typo, or a retired dated id |

Note the first one is a `ResourceNotFoundException`, not `AccessDeniedException`.
Searching for "AccessDenied" in the container logs will miss it.

**One place in the repo carries an id that is already invalid:**
`workshop-simulation/existing-ec2-agent/agent.py:13` pins
`anthropic.claude-sonnet-4-20250514`, which returns
`The provided model identifier is invalid.` today. The `agent-code/*` defaults
(`us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-opus-4-6-v1`) were still
resolving when this was measured — but set `MODEL_ID` explicitly rather than
finding out live.

### The deploy stops asking for an Organizations id

`enable_resource_policies=true` renders `aws:PrincipalOrgID` into the Memory
resource policy and cannot be built without it:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
```

Interactively the script prompts for an `o-xxxx` value and only fails if you
leave it empty; with `NON_INTERACTIVE=1` it is a hard stop with the fix printed.
If the account is not in an Organization, leave `enable_resource_policies` off —
which means not using the `security-focused` profile as shipped.

**This gate also fires in `--dry-run`, before the plan is printed.** So
`workshop --dry-run --profile security-focused` produces no plan at all without
`ORG_ID`: with a terminal it stops and waits at the prompt, and with stdin closed
it warns and exits 1. To *preview* the plan, any `o-…`-shaped value works, because
dry-run makes no AWS calls:

```bash
ORG_ID=o-preview0 ./scripts/deploy.sh workshop --dry-run --profile security-focused
```

Different failure mode, same variable: `enable_networking=true` **without**
`ORG_ID` does not fail. It warns and creates the AgentCore VPC endpoint with **no
policy at all**. Treat that warning as an error anywhere it matters.

### `The following subnets are in unsupported availability zones in region …`

The runtime redeploy fails, not the networking deploy:

```
Reason: The following subnets are in unsupported availability zones in region
us-east-1: subnet-0fa4… in us-east-1a (ID: use1-az6). Supported availability
zones are: use1-az4, use1-az1, use1-az2
HandlerErrorCode: NotStabilized
```

**The message is completely accurate — trust it.** It rolls back cleanly
(`UPDATE_ROLLBACK_COMPLETE`, ~90s) and the runtime keeps working in `PUBLIC` mode,
so nothing is wedged. You just do not have a VPC deployment yet.

Why the networking stack was green: AgentCore supports a limited set of AZ **ids**
per Region, `networking_stack.py` asks CDK for `max_azs=2` which takes the first two
AZ *names* alphabetically, and name → id mapping differs per account. Nothing
validates this at deploy time — `unsupported_zone_ids()` is called only by
`check_network.py`. Catch it before the runtime does:

```bash
.venv/bin/python scripts/check_network.py
aws ec2 describe-availability-zones --region "$AWS_REGION" \
  --query 'AvailabilityZones[].{Name:ZoneName,Id:ZoneId}' --output table
```

**The fix is a source edit plus a destroy — not a flag and not a redeploy.** There
is no context key, env var or `platform.yaml` entry for AZs. In
`stacks/networking_stack.py`, replace `max_azs=2` with the names that map to
supported ids *in this account*:

```python
availability_zones=["us-east-1b", "us-east-1c"],
```

Then read the next entry before you run `deploy`.

### `The CIDR '10.0.2.0/24' conflicts with another subnet` after changing AZs

```
Resource handler returned message: "The CIDR '10.0.2.0/24' conflicts with another
subnet …" HandlerErrorCode: AlreadyExists
```

Changing AZs forces subnet **replacement**, and CloudFormation creates the
replacements before deleting the originals — so they collide with their own CIDRs
and all four subnets fail. The stack rolls back to its previous, still-broken AZs.

`check_network.py`'s advice to "redeploy the networking stack" is not sufficient.
**Destroy it first:**

```bash
./scripts/deploy.sh destroy --stack "$PREFIX-networking"     # ~4m30s
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module C   # ~3m30s
```

Safe to destroy if no runtime ever entered the VPC — no `agentic_ai` ENIs exist to
block it. If runtimes *were* in the VPC, see "Destroying the networking stack fails
on subnets or security groups" below.

### `check_network.py`: `networkMode is PUBLIC, not VPC — the VPC exists but the agent is not in it`

Working as intended, and the most common surprise in module C. **Deploying the
networking stack does not move existing runtimes into it.** You have a VPC, a NAT
gateway and six endpoints that nothing is using.

```bash
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module 6   # ~345s
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module 8   # ~172s
```

Note the corollary: **`check_network.py --expect-public` will pass** in this state,
because the agents genuinely are public. That is not confirmation you are safe.

`check_network.py` also stops at its first failure, so on a fresh account expect two
rounds — AZ ids first, then placement.

### `workshop-outputs-<stamp>.json` has no usable stack outputs

`stack_outputs` is a list of single characters and every `OutputKey` is missing.
`./scripts/deploy.sh export` merges each stack's JSON by splitting on whitespace and
re-parsing the fragments, so the documents get shredded; the failures are swallowed
by `except: pass`. Measured: 769 one-character entries, zero recoverable
key/value pairs.

The mechanism is worth knowing, because it decides what does and does not end up in
the file. Splitting pretty-printed JSON on whitespace yields tokens like
`"OutputKey":` and `"someValue",` — both fail `json.loads` because of the trailing
colon or comma. The only tokens that parse are bare quoted strings, i.e. the **last
field of each output object**, and `result.extend()` on a parsed string appends its
characters one at a time. So each output contributes at most its final field's
characters, in order, concatenated with its neighbours' — which is why rejoining the
list gives a readable but unusable run of ARNs, URLs, ids and CloudFormation export
names with no separators.

**The `ssm_parameters` half of the file is fine** — one API call, no merge, and it
carries no secrets. For stack outputs, query CloudFormation directly:

```bash
aws cloudformation describe-stacks --stack-name "$PREFIX-gateway" \
  --query 'Stacks[0].Outputs' --output json
```

Also note the file lands in the repo root and is not gitignored.

### `invoke.py` printed hundreds of `data: {…}` lines instead of an answer

Not a crash, and not a deployment problem. **`invoke.py` prints whatever the pattern
emits**, and `langgraph-agent` (and the other streaming patterns) emit raw SSE — one
`data:` chunk per few tokens. Measured: 176 lines for a one-sentence reply, exit code
0. The `orchestrator` pattern prints a single `{"status": "success", "response": …}`
line, which is why the difference reads as a regression.

The text is in the `content[].text` fields. To read it as a sentence:

```bash
.venv/bin/python scripts/invoke.py "…" \
  | python3 -c 'import sys,json
print("".join(p["text"] for l in sys.stdin if l.startswith("data: ")
  for p in (json.loads(l[6:]).get("content") or []) if isinstance(p,dict) and p.get("text")))'
```

Do **not** reach for `--agui` — that is for the `agui-*` patterns and fails on
protocol against an `HTTP` runtime.

### A memory-using pattern recalls within a session but not across sessions

Working as configured. Measured on `langgraph-agent` with Memory `ACTIVE`: recall in
the same `--session` worked; the same question in a new session returned `NO RECORD`.

The events are being stored — `list-sessions` shows them. What is missing is a
strategy that turns them into recallable facts:

```bash
aws bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
  --query 'memory.strategies[].{type:type,status:status}'
#   → [{"type": "USER_PREFERENCE", "status": "ACTIVE"}]      ← no semantic strategy
```

Semantic fact extraction only exists when `use_long_term_memory=true`, and it defaults
to `false` (`app.py:142-143`) because it costs more:

```bash
USE_LONG_TERM_MEMORY=true ./scripts/deploy.sh deploy --module A
```

Related: if you are checking whether *anything* was written, look under the right
actor. For `invoke.py` (M2M) the `actor_id` is the **app client id**, not a username —
`list-actors` returns the value of `/{project}/{env}/auth/m2m-client-id`.

### I changed agent code and the redeploy changed nothing

Image tags are a content hash of the source plus the selected pattern, and
CodeBuild only reruns when the hash changes. Identical tags across two deploys
means no rebuild happened:

```bash
aws ecr describe-images --repository-name $PREFIX-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-5:].{tags:imageTags,pushed:imagePushedAt}'
```

If you expected a rebuild, confirm you edited a file **inside the build context**
(`agent-code/`), not something excluded from it.

**The timing is the giveaway.** A module 6 redeploy with nothing changed is a
27.7s no-op end to end — measured — and every stack in the chain reports
`✅ <stack> (no changes)` with `Deployment time: 0s`. If your "rebuild" came back
in under a minute, it did not build anything. A real rebuild goes quiet for
minutes while CodeBuild works. So this cuts both ways: the same absence of output
that means "nothing happened" here means "be patient" on a first deploy.

### `platform.yaml is invalid`

Validation is deliberately strict and reports **every** problem at once, before
any AWS call. Unknown keys are errors, not no-ops — a typo would otherwise
silently do nothing.

```bash
.venv/bin/python -m infra_utils.platform_config platform.yaml
```

### Old answers keep coming back

The wizard remembers answers in `workshop.env`, and `platform.yaml` overrides it.

```bash
./scripts/deploy.sh config           # effective values, with their source
./scripts/deploy.sh config --reset   # delete saved answers
```

### Module 6 has been silent for eight minutes

Not a hang. CodeBuild is building an **arm64** container image remotely. Expect
~7 minutes on a first build. Confirm if you want to see it move:

```bash
aws codebuild list-builds-for-project --project-name $PREFIX-build-orchestrator \
  --query 'ids[0]' --output text
```

or just watch ECR for a new pushed image.

### I pressed Ctrl-C during a deploy

**The script stops. AWS does not.** Measured, interrupting a module 6 rebuild
while CloudFormation was mid-update:

| What | What happened |
|---|---|
| the script and the CDK CLI | both gone immediately, no grace period, no "stopping deployment" |
| what the log said | `[ERROR] Failed to deploy <stack>` / `[ERROR] Check CloudFormation console for details.` — **byte-identical to a real failure** |
| the CloudFormation stack | still `UPDATE_IN_PROGRESS`, then `UPDATE_COMPLETE` on its own |
| the CodeBuild build | ran to `SUCCEEDED` |
| the runtime | `agentRuntimeVersion` bumped, `status: READY` |

So the interrupted deploy **succeeded**, and the only thing that failed was the
script's ability to watch it. Do not react to that error text — ask AWS:

```bash
aws cloudformation describe-stacks --stack-name $PREFIX-runtime-orchestrator \
  --query 'Stacks[0].[StackStatus,LastUpdatedTime]' --output text
```

- `UPDATE_COMPLETE` / `CREATE_COMPLETE` → nothing to do. A re-run came back
  `(no changes)` in 27.6s.
- `UPDATE_IN_PROGRESS` → **wait.** Do not retry; a second `cdk deploy` against a
  stack that is mid-update cannot proceed. Poll the command above.
- `UPDATE_ROLLBACK_COMPLETE` → safe to deploy onto again; re-run the same command.

The one thing an interrupt can genuinely cost you is a **guided run's place in the
sequence**, since the loop dies with the script. Resume with
`workshop --from <module>` — the modules before it are skipped explicitly, one
`Skipping module N` line each, and the dependency stacks it walks through come back
`(no changes)`.

### I passed `--dry-run` and it deployed anyway

**First: which action did you run it on?** `--dry-run` is only honoured by
`workshop`. The `deploy` case reads `NON_INTERACTIVE`, then calls `cdk_bootstrap`
and deploys — it never tests `DRY_RUN` at all (`scripts/deploy.sh:837-865`). So
`deploy --module 3 --dry-run`, spelled perfectly, is a real deploy. Measured
against a Region that had never been touched: `Total time: 58.51s`, a `CDKToolkit`
bootstrap stack, a live Cognito user pool with three app clients and a hosted
domain, and the M2M client secret printed in the summary table. No warning.

If that is what happened, tear it down before anything else, and remember the
Region — `destroy` only knows about the Region you point it at:

```bash
AWS_REGION=<that-region> ./scripts/deploy.sh destroy
```

Then sweep for the CDK staging bucket and any `/aws/lambda/${PREFIX}…` log groups;
see "Tearing down" in `deploy.md`. To actually preview: `workshop --dry-run`, or
`synth` (renders templates, creates nothing).

**Second: check the spelling.** The argument parser drops anything it does not recognise —
**silently, with no warning** — so `--dryrun`, `--dry_run` or `--dryRun` leaves
`DRY_RUN=0` and the run is real. Same for `--modul 6`, `--profil greenfield`,
`--frm 6`: the flag vanishes and its value is swallowed with it, so the deploy
proceeds unscoped.

Flag names are never validated. Flag **values** are validated unevenly, and the
two gaps both fail the same expensive way — by deploying the whole app:

| Value | Bad value does what |
|---|---|
| `--module` | hard error, valid list printed (`scripts/deploy.sh:812`) |
| `--from` | hard error (`:872`) |
| `AGENT_PATTERN` | hard error, valid list printed (`:139-141`) |
| `--profile` | hard error **only on `workshop`** (`:779`) — on `deploy` it is ignored |
| `--team` | **never validated** |

An unvalidated bad value falls past the `elif` chain that sets `CDK_STACKS`
(`:810-817`), leaving it empty, and an empty stack list means `cdk deploy --all`.
So `deploy --team platfrom` and `deploy --profile greenfeild` each deploy every
stack the app defines, with default feature flags, and report success. Confirm
from the echoed `Workshop Module … → Stacks:` or `Team … → Stacks:` line that the
scope was actually applied.

Read the output rather than trusting what you typed. A real dry run prints both of
these, and no `═══ Deploying ═══` header after them:

```
[INFO]  Dry run: skipping prerequisite and credential checks (no AWS calls)
[INFO]  DRY RUN — nothing will be deployed
```

The `═══ Prerequisite Checks ═══` banner and your account id mean AWS calls are
happening — which is normal for `synth`/`diff` but means a `deploy` is underway.

### I deployed far more than I expected

First rule out a misspelled `--module` / `--team` (above) — the value is dropped
with the flag.

Then: `--profile` sets the profile's **feature flags** and then runs
`cdk deploy --all` — every stack the app defines, not just the profile's modules.
Scope it:

```bash
./scripts/deploy.sh deploy --profile <p> --module <n>
./scripts/deploy.sh deploy --profile <p> --team <t>
./scripts/deploy.sh workshop --profile <p>          # module-by-module
```

Extra stacks are not dangerous, but with a networking profile they can be
expensive. Check what landed with
`aws cloudformation describe-stacks --query "Stacks[?starts_with(StackName,'$PREFIX')].StackName"`
and destroy what you did not want.

---

## The agent does not work

### Invoke returns `Unauthorized`

Applies to the patterns that verify identity themselves — `strands-agent`,
`langgraph-agent`, `claude-sdk-*`, `agui-*` (via `agent-code/shared/auth.py`).
**The default `orchestrator` pattern reads no token at all, so it never produces
this.**

Those agents check the caller's JWT — signature against the issuer's JWKS pinned
to RS256, plus expiry, issuer and client — rather than trusting that the runtime
authorizer ran. The response is deliberately generic; **the reason is in the
container logs.** Common causes:

- `COGNITO_ISSUER_URL` / `COGNITO_ALLOWED_CLIENTS` not injected into the runtime
  → the agent refuses rather than decoding unverified.
- A token from a different user pool, or an expired one.
- An M2M token whose client id is not in the allowed clients list.

One asymmetry worth knowing: a missing **issuer** is a hard reject, but an
**empty** `COGNITO_ALLOWED_CLIENTS` degrades quietly to "any client of the
correct issuer." Set both.

### Invoke returns HTTP 424

**424 means the container is serving the wrong protocol — not that you sent the
wrong payload.** The repo's own `docs/TROUBLESHOOTING.md` and the docstring at
`scripts/invoke.py:112` say a `{"prompt": ...}` body to an A2A runtime "gets you a
424." Measured against a working `code-agent`, it does not; see the next section.
Keep the two apart or you will redeploy a healthy image:

| Cause | Symptom | Fix |
|---|---|---|
| Image built on `BedrockAgentCoreApp` (HTTP `/invocations`, port 8080) while the runtime is registered `A2A` | **424**, clean container logs | rebuild on `agent-code/shared/a2a_serve.py` |
| Right image, wrong envelope from the client | **200** + JSON-RPC `-32600` | fix the call, use `invoke.py --a2a` |

The 424 case is a real defect this repo shipped once — stacks clean, logs clean,
every invoke 424 — which is why `tests/test_a2a_contract.py` now guards it
statically. If a sub-agent you wrote 424s, check it serves the contract: `POST /`
(JSON-RPC), `GET /.well-known/agent-card.json`, `GET /ping` returning
`{"status": "Healthy"}` — all on `0.0.0.0:9000`.

### An A2A invoke returns HTTP 200 and the agent never answered

Read the body. A correctly built A2A sub-agent accepts the connection and rejects
the *envelope* at the JSON-RPC layer, so the HTTP status is 200 and the failure is
inside the payload:

```json
{"jsonrpc":"2.0","error":{"code":-32600,"message":"Request payload validation error",
 "data":[{"type":"missing","loc":["method"],"msg":"Field required",
          "input":{"prompt":"…"}}]}}
```

`missing field: method` with your `{"prompt": …}` echoed back as `input` is the
tell: you sent the orchestrator's HTTP shape to a JSON-RPC endpoint. A2A runtimes
want `message/send`. Do not go looking for a 424 and do not redeploy — use the path
that builds the envelope for you:

```bash
.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"
```

Anything hand-rolling `invoke_agent_runtime` against a sub-agent needs the JSON-RPC
envelope **and** SigV4 (not a Bearer token) **and** a `runtimeSessionId` of at least
33 characters.

Otherwise compare the runtime's protocol against what its code serves:

```bash
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> \
  --query '[agentRuntimeVersion,protocolConfiguration]'
```

Expected: A2A sub-agents report `A2A`, an MCP-server runtime `MCP`, the
orchestrator `HTTP` — except `agui-*` patterns, which report `AGUI`.

### `Invalid length for parameter runtimeSessionId`

```
botocore.exceptions.ParamValidationError: Parameter validation failed:
Invalid length for parameter runtimeSessionId, value: 32, valid min length: 33
```

Twenty lines of traceback, exit 1, and no AWS call was made — this is client-side
botocore validation. **Read the last line; the message is accurate.** Session ids
must be **≥33 characters** and `invoke.py` does not enforce it, so `--session` lets a
short one through.

What makes this confusing is that the two invoke paths disagree, and the strict one
is the one people reach for second:

| Path | 32-char session id |
|---|---|
| orchestrator (`invoke.py --session`) | **accepted** — travels in the request body |
| A2A (`invoke.py --a2a --session`) | **rejected** — goes to boto3 `InvokeAgentRuntime` |

So an id that has worked all week starts failing the first time someone points it at
a sub-agent. Use something safely long everywhere:

```bash
.venv/bin/python scripts/invoke.py --a2a code-agent --session "session-$(uuidgen | tr -d -)" "…"
```

### `Authorization method mismatch` on invoke

The runtime's inbound auth and your request disagree, and it cuts **both** ways:

| Runtime | Expects | Call it with |
|---|---|---|
| orchestrator (`HTTP`/`AGUI`/`MCP`) | Bearer JWT (CUSTOM_JWT authorizer) | `invoke.py` / `invoke.py --agui` |
| A2A sub-agents (`A2A`) | **SigV4** — no authorizer; IAM `InvokeAgentRuntime` | `invoke.py --a2a <component>` |

A2A is not a client-facing protocol, so those runtimes deliberately get no JWT
authorizer. Sending a bearer token to one is rejected exactly like sending SigV4
to the orchestrator. `scripts/invoke.py` picks the right mechanism per target.

### The agent reports no tools

**First: is this the `orchestrator` pattern?** It ships with no tools by design.
Tools are consumed by `strands-agent`, `langgraph-agent`, `claude-sdk-*` and
`agui-*`.

Otherwise gateway tools load through AgentCore Identity — the agent exchanges M2M
credentials for a gateway token via the token vault. Two failure shapes, and
telling them apart saves real time:

- **Silently no tools** — the agent answers, just without them. Only two
  conditions do this, both `return None` in `create_gateway_mcp_client()`
  (`agent-code/strands-agent/tools/gateway.py:79-93`, duplicated per
  tool-consuming pattern): `GATEWAY_CREDENTIAL_PROVIDER_NAME` unset, or the
  gateway URL unresolvable.
  Check both env vars on the runtime and look for `[GATEWAY]` warnings in the
  container logs — the client logs why it gave up.
- **The invoke fails outright** — if the runtime role cannot read the vault's
  secret (`secretsmanager:GetSecretValue` on
  `bedrock-agentcore-identity!default/oauth2/*`), the token fetch raises
  AccessDenied inside the MCP client and the agent aborts rather than degrading.
  An invoke that *errors* instead of *answering* points here.

Then confirm the gateway side is healthy and the tool is registered:

```bash
.venv/bin/python scripts/test_gateway.py
```

Also: the built-in web-search connector only exists in some Regions and is
Region-gated in `app.py`, so in an unsupported Region the tool is absent **by
design**.

A tool that is listed but whose calls fail is usually a missing gateway-role
action for the connector, or a missing Lambda invoke permission. A
`Unsupported tool: …` error from your own Lambda means the handler is dispatching
on the full `target___tool` name instead of the suffix after `___`.

### A memory-backed pattern fails at invoke

Framework memory integrations call data-plane actions — LangGraph's checkpointer
lists events to rehydrate a thread. If the **runtime role** lacks `ListEvents` /
`CreateEvent` / `RetrieveMemoryRecords`, the pattern fails at invoke while the
stack looks fine.

`scripts/test_memory.py` cannot tell you this: it uses **your local
credentials**, so it passes regardless. Check the role directly:

```bash
aws iam list-role-policies --role-name <project>-orchestrator-role
```

---

## Observability

### Module 9's verify fails with `trace segment destination is CloudWatchLogs but status is PENDING`

```
Checking /agentcore-workshop/dev in us-east-1
FAIL: trace segment destination is CloudWatchLogs but status is PENDING
```

Nothing is broken. Enabling Transaction Search is **asynchronous and slower than the
stack that requests it**, and `check_observability.py` runs about a second after
`CREATE_COMPLETE`. It has no retry, so on a first-ever enablement in an account it
fails by design of the timing, not of the platform.

Measured on a fresh account: stack complete at `15:06:19Z`, still `PENDING` two
minutes later, `ACTIVE` at `15:14:41Z` — **8m22s**. Watch it directly rather than
guessing:

```bash
aws xray get-trace-segment-destination --region $AWS_REGION
# {"Destination": "CloudWatchLogs", "Status": "PENDING"}  → wait
# {"Destination": "CloudWatchLogs", "Status": "ACTIVE"}   → re-run the verify
```

Then simply re-run it — it passes with no redeploy:

```
PASS: trace segment destination is CloudWatchLogs (ACTIVE)
PASS: X-Ray span delivery policy present (…-transaction-search-xray)
PASS: 3 vended log deliveries active
OK: logs and traces are being accepted
```

Two consequences worth planning around. In a guided run this is the **last** module
of `greenfield`, so the session's final step is the one most likely to go red —
pre-empt it. And under `NON_INTERACTIVE=1` a failed verify aborts the run
(`Aborting (NON_INTERACTIVE=1 cannot prompt)`), so an automated walk of the full
sequence needs either a wait before module 9 or a re-run afterwards.

Only the first enablement in an account is slow. Once `ACTIVE`, it stays that way —
including after teardown.

### No traces appear anywhere

Runtimes emit OTLP spans even when the account cannot receive them: while the
X-Ray trace segment destination is still `XRay`, **every batch is rejected with
HTTP 400 and the deployment still reports success.** The observability stack sets
the destination, so the usual cause is deploying with
`enable_transaction_search=false`.

```bash
.venv/bin/python scripts/check_observability.py
aws xray get-trace-segment-destination      # expect CloudWatchLogs / ACTIVE
```

### `batch-get-traces` returns nothing for my trace

Expected, not a failure. With Transaction Search **all** spans are searchable in
the `aws/spans` log group, while the classic X-Ray APIs only serve the indexed
sample (default rule: 1%). Search the way the console does:

```bash
.venv/bin/python scripts/check_observability.py --spans     # needs an invoke in the last hour
```

Also give it time — span delivery lags an invocation by a minute or two, which is
exactly why `--spans` is not part of module 9's verify.

---

## Teardown

### `destroy --stack` fails with "Export … cannot be deleted"

Working as intended, and **nothing was deleted**. Measured on
`destroy --stack $PREFIX-auth` with three consumers standing: exit 1 after 22s, and
the stack went `DELETE_IN_PROGRESS` → straight back to `CREATE_COMPLETE`.

```
agentcore-…-auth | 1 | CREATE_COMPLETE | AWS::CloudFormation::Stack
  Delete canceled. Cannot delete export agentcore-…-auth:ExportsOutputRefUserPool…
  as it is in use by agentcore-…-gateway, agentcore-…-identity and
  agentcore-…-runtime-orchestrator.
[ERROR] If the error above names an export in use, another stack depends on this one.
[ERROR] Destroy the dependents first, or run 'destroy --all' for the whole environment.
```

A targeted destroy passes `--exclusively`, so CloudFormation refuses rather than
cascading into the dependents — which would take the platform out from under other
teams. **The message names every consumer**, which makes it a dependency-discovery
tool as much as an error: run it to find out who depends on a stack before you plan
a change. Either destroy those first, or take the whole environment down:

```bash
./scripts/deploy.sh destroy        # cascade is intended here
```

One footnote on that error text: `--all` is not a real flag. The parser ends in
`*) shift ;;`, so `destroy --all` silently drops it and works only because a bare
`destroy` already destroys everything. Do not go looking for `--all` elsewhere.

### `destroy` exited non-zero and half the platform is still standing

**Expected, and the half still standing is the expensive half.** A bare `destroy`
runs `cdk destroy --all --force` (`scripts/deploy.sh:590-595`), and CDK **stops at
the first stack it cannot delete** rather than skipping it and continuing.

Because the order is reverse-dependency — observability → runtime stacks → gateway,
memory, identity, auth, security → **networking last** — a failure in an early,
cheap runtime stack leaves the NAT gateway and all five interface endpoints running.
Measured: 693s, failed on the fourth of ten stacks, exit 1, six stacks left up
including `-networking`, NAT still `available`.

```bash
# What is left
aws cloudformation list-stacks \
  --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" \
  --output text | sort

# Is anything still on an hourly meter?
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].NatGatewayId' --output text
```

Clear the failed stack (next entry is the usual cause), then **re-run
`./scripts/deploy.sh destroy`** to take out the remainder.

### `DELETE_FAILED`: "Request timed out while deleting AWS::BedrockAgentCore::Runtime"

```
DELETE_FAILED (The following resource(s) failed to delete: [Runtime]. ):
Resource handler returned message: "Request timed out while deleting
AWS::BedrockAgentCore::Runtime" (HandlerErrorCode: NotStabilized)
```

**`NotStabilized` means CloudFormation gave up waiting, not that the delete
failed.** In the measured case the runtime was already gone at the service level
while the stack sat in `DELETE_FAILED`. Confirm that before doing anything drastic —
if the list is empty, there is nothing left to clean up by hand:

```bash
aws bedrock-agentcore-control list-agent-runtimes \
  --query 'agentRuntimes[].[agentRuntimeName,status]' --output text
```

Then just retry the delete. **Measured: 34 seconds to `DELETE_COMPLETE`** on a stack
that had timed out minutes earlier.

```bash
aws cloudformation delete-stack --stack-name "$PREFIX-runtime-<component>"
aws cloudformation wait stack-delete-complete --stack-name "$PREFIX-runtime-<component>"
```

Ignore the handler's advice to "delete it from the AWS console" — a plain retry is
enough, and the console does the same call. Afterwards re-run
`./scripts/deploy.sh destroy` for the stacks the abort skipped.

### Destroying the networking stack fails on subnets or security groups

**It fails fast and then you wait — those are two different clocks.** Measured: the
stack reached `DELETE_FAILED` about 17 minutes after the delete started. It does not
sit in `DELETE_IN_PROGRESS` for hours. The ~8 hours is how long the ENIs may take to
release before a *retry* will succeed.

The three failure reasons, verbatim, so they are searchable:

```
RuntimeSecurityGroup   resource sg-… has a dependent object (Service: Ec2, Status Code: 400)
VPCPrivateSubnet1      The subnet 'subnet-…' has dependencies and cannot be deleted.
VPCPrivateSubnet2      The subnet 'subnet-…' has dependencies and cannot be deleted.
```

Neither message mentions ENIs, AgentCore, or runtimes — which is why this reads as a
mystery the first time. The "dependent object" is an `agentic_ai` ENI.

AgentCore leaves those ENIs behind for up to ~8 hours after runtimes stop
using a VPC. Measured: both ENIs still `in-use` after every runtime had been deleted
and `list-agent-runtimes` came back **empty** — the ENI outlives the runtime that
created it by hours.

```bash
aws ec2 describe-network-interfaces \
  --filters Name=interface-type,Values=agentic_ai \
  --query 'NetworkInterfaces[].{id:NetworkInterfaceId,status:Status,subnet:SubnetId}'
```

**Exactly three resources get stuck, and they are the cheap ones:** the runtime
security group (the ENIs hold it) and the two private subnets. Measured on a real
teardown, at the same moment: NAT gateway `deleted`, all five interface endpoints
gone, public subnets gone — only `RuntimeSecurityGroup` and
`VPCPrivateSubnet{1,2}` left `DELETE_IN_PROGRESS`.

**Do not try to delete the ENIs by hand.** They report
`RequesterManaged: false` and an empty `Description`, which makes them look like
ordinary account-owned interfaces, and they are not:

```
$ aws ec2 delete-network-interface --network-interface-id eni-…
InvalidParameterValue: Network interface 'eni-…' is currently in use.
```

The attachment is an `ela-attach-…` with `InstanceId: None` and
`InstanceOwnerId: amazon-aws` — a service-managed attachment with no instance to
detach from, so `detach-network-interface` is not an option either. There is no
force. The only move is to come back later and re-run the destroy.

The filter is the only reliable way to find them, since `Description` is blank and
`RequesterManaged` lies: match on `interface-type=agentic_ai`.

If `-networking` is the *only* stack left, NAT and the endpoints went with the rest
of it and the wait costs nothing meaningful. **Confirm that it really is the only one
left** — if the destroy aborted earlier in the sequence, networking was never
attempted and the hourly meters are still running. See the abort entry above.

**Do not let the drain hold the rest of the teardown hostage.** `cdk destroy --all`
works through stacks serially, so while it sits on `-networking` it has not yet
touched `-auth`, `-gateway`, `-identity` or `-security` — none of which depend on
networking. Leaving `-auth` up is the part that matters, because that is the stack
whose CloudFormation export carries the M2M client secret in plaintext.

Take them out directly instead of waiting. Dependents of `-auth` first, then `-auth`:

```bash
for s in gateway identity security; do
  aws cloudformation delete-stack --stack-name "$PREFIX-$s"
done
for s in gateway identity security; do
  aws cloudformation wait stack-delete-complete --stack-name "$PREFIX-$s"
done
aws cloudformation delete-stack --stack-name "$PREFIX-auth"
aws cloudformation wait stack-delete-complete --stack-name "$PREFIX-auth"
```

Measured: all four gone, `-auth` in 34s, leaving only `-networking` waiting on its
ENIs. Then re-run `./scripts/deploy.sh destroy` later to finish networking.

### Deleting the observability stack did not disable Transaction Search

Deliberate. The trace segment destination is account- and Region-scoped, and
other workloads may depend on it by the time you tear this down. Revert it
yourself if you really want to:

```bash
aws xray update-trace-segment-destination --destination XRay
```

---

## Still stuck

Collect these first — they answer the first three questions anyone will have:

```bash
./scripts/deploy.sh config
aws sts get-caller-identity
aws cloudformation describe-stacks \
  --query "Stacks[?starts_with(StackName,'$PREFIX')].{n:StackName,s:StackStatus}" --output table
./scripts/deploy.sh verify
```
