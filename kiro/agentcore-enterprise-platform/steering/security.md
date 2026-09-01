# Security controls

Read this when a security, network, or compliance reviewer is in the room — or
before anyone claims this deployment is hardened.

**Two sentences to lead with, because they set every expectation correctly:**

1. **Every security control is off by default.** The defaults are a working
   platform, not a hardened one. Two flags elsewhere do default on —
   `enable_transaction_search` (tracing does not work without it) and
   `enable_a2a`.
2. **`$PREFIX-security` reaching `CREATE_COMPLETE` means resources exist, not
   that anything is being enforced.** Module E's verify is a stack-status check
   precisely because there is no behavioural probe for "is this control working."

The credibility of this accelerator in a security review comes from the second
point. Say the honest version before a reviewer finds it.

One finding does not wait for a flag and is not hypothetical: the `-auth` stack
publishes the **Cognito M2M client secret in plaintext** as a CloudFormation
export. Read "The Cognito M2M client secret is a plaintext CloudFormation export"
below before any review, and before putting a deploy on a shared screen.

---

## The scope-split model

One control library, two engines. Every control is authored **once** as valid
JSON or Cedar in `control-library/`, indexed by `catalog.yaml`, with
`<<sentinel>>` parameters injected at deploy time.

| | Owns | Reads from |
|---|---|---|
| **Terraform** `terraform/org-guardrails/` | org scope — SCPs | `control-library/scp/` |
| **CDK (Python)** | account/workload scope — resource policies, VPCE policy, Cedar, Guardrails, interceptor | `control-library/` via `infra_utils/policy_loader.py` |

Two consequences worth naming:

- A policy fix lands in both engines at once — no duplicated policy bodies
  drifting across languages.
- Because the artifacts are **valid JSON, not templated `.tftpl`**, checkov,
  IAM Access Analyzer and cfn-guard can scan every file in CI. Sentinels are
  `<<name>>` specifically so they never collide with IAM policy variables
  (`${aws:...}`) or Terraform `templatefile()` syntax.

That portability is the answer to "we don't use Terraform": the same JSON goes
into Control Tower custom controls, AFT customizations, CloudFormation StackSets
(`AWS::Organizations::Policy`), or a console paste. Terraform is the path this
repo ships and tests; the JSON is what the customer keeps.

```bash
make validate-controls    # control-library ↔ catalog.yaml consistency
make test-controls        # pytest tests/ -q
```

---

## What the library actually contains

15 controls in `catalog.yaml`. Read this table as the inventory to walk a
reviewer through:

| Control id | Type | Scope | Applied by |
|---|---|---|---|
| `scp.memory.enforce-cmk` | SCP | org | Terraform |
| `scp.identity.deny-workload-token-for-userid` | SCP | org | Terraform |
| `scp.gateway.require-cmk` | SCP | org | Terraform *(merged)* |
| `scp.gateway.deny-no-auth` | SCP | org | Terraform *(merged)* |
| `scp.gateway.require-policy-engine` | SCP | org | Terraform *(merged)* |
| `scp.gateway.enforce-approved-idp` | SCP | org | Terraform *(merged)* |
| `scp.gateway.restrict-protocol` | SCP | org | Terraform *(merged)* |
| `scp.gateway.targets-require-private-endpoint` | SCP | org | Terraform *(merged)* |
| `scp.gateway.targets-restrict-credential-provider` | SCP | org | Terraform *(merged)* |
| `scp.gateway.targets-restrict-type` | SCP | org | Terraform *(merged)* |
| `resource-policy.memory.in-account-only` | resource policy | workload | CDK, `enable_resource_policies` |
| `vpce.agentcore-in-org` | VPCE policy | account | CDK, `enable_networking` + `org_id` |
| `guardrail.egress-default` | Guardrail | workload | CDK, `enable_egress_filter` |
| `cedar.gateway-default.permit-read` | Cedar | workload | CDK, `enable_cedar` |
| `iam.runtime-execution-least-privilege` | IAM | account | **nothing — reference only** |
| `iam.identity-credential-provider-scoped` | IAM | account | **nothing — reference only** |

The last two are templates a team adopts, not controls the deploy applies. Their
`engine: [cdk]` field describes intent, not behaviour. `infra_utils/agentcore_role.py`
points at the first one in a comment and does not read it. Do not present them as
deployed.

Deploy the whole workload/account set at once:

```bash
export ORG_ID=$(aws organizations describe-organization --query Organization.Id --output text)
./scripts/deploy.sh deploy --profile security-focused --module E
```

Or one control at a time (note `NON_INTERACTIVE` is read by `deploy.sh`, not by
`cdk`):

```bash
cdk deploy agentcore-workshop-dev-gateway -c enable_cedar=true
```

---

## Cedar — say what it does, not what "policy engine" implies

`enable_cedar=true` attaches a `CfnPolicyEngine` to the gateway and loads one
Cedar policy from the library. **Three things must all be true before "default-deny
authorization on tool calls" is a fair description, and by default none of them
are:**

1. `enable_cedar` defaults **off** — no engine, no evaluation at all.
2. `cedar_mode` defaults to **`LOG_ONLY`** — decisions are logged, nothing is
   denied.
3. The single shipped permit is **unconstrained on principal and resource**:

   ```
   permit(principal, action in [AgentCore::Action::"sample-tool___text_analysis_tool"], resource);
   ```

   Any authenticated caller may invoke the sample tool on any gateway. Narrow the
   principal and resource before enforcing.

What *is* genuinely strong: **Cedar is implicit default-deny.** No permit means
denied, so the library ships permits only — deliberately no blanket `forbid`,
because in Cedar a matching forbid overrides every permit unconditionally and
would make the permit dead code and deny everything. Keep any forbid you add
narrow.

The permitted action name is the full gateway tool name,
`<TargetName>___<tool_name>` (three underscores). The default comes from
`catalog.yaml` (`read_action`, default `sample-tool___text_analysis_tool`).
**Every tool you add is denied once `cedar_mode=ENFORCE` unless a permit names
it** — that is the correct behaviour and also the most common "we broke the demo"
moment.

Safe rollout, in this order:

```bash
# 1. Attach in LOG_ONLY and generate traffic
./scripts/deploy.sh deploy --module 5 -c enable_cedar=true
.venv/bin/python scripts/test_gateway.py
.venv/bin/python scripts/invoke.py "Use the text analysis tool on 'hello world'."

# 2. Read the decision logs. Confirm what would have been denied.
# 3. Narrow the permit's principal and resource in
#    control-library/cedar/gateway-default/10-permit-read-tools.cedar
# 4. Only then:
./scripts/deploy.sh deploy --module 5 -c enable_cedar=true -c cedar_mode=ENFORCE
```

Never enable `ENFORCE` before step 2. And remember flags are matched against the
exact lowercase string `"true"` — `-c enable_cedar=True` silently does nothing.

---

## Egress filter — masking, not blocking

`enable_egress_filter=true` creates a Bedrock Guardrail from
`control-library/guardrails/egress-default.json` and a Lambda
(`$PREFIX-egress-interceptor`, from `tools/egress_interceptor/`) registered as a
gateway interceptor on both `REQUEST` and `RESPONSE`, with
`pass_request_headers=False` — the interceptor never sees caller tokens.

The shipped guardrail config:

| Policy | Setting |
|---|---|
| Prompt attack filter | input `HIGH`, output `NONE` |
| `EMAIL`, `PHONE`, `NAME`, `ADDRESS` | `ANONYMIZE` |
| `US_SOCIAL_SECURITY_NUMBER`, `CREDIT_DEBIT_CARD_NUMBER` | `BLOCK` |

Four caveats to state plainly, because a reviewer will ask:

- **It masks; it rarely blocks.** The handler raises only when an assessment comes
  back `action == "BLOCKED"`. Otherwise it substitutes anonymized text and the
  request proceeds.
- **It performs no authorization.** That is Cedar, behind a separate flag.
  `enable_egress_filter` alone gives you none of it.
- **Unrecognised payload shapes pass through unchanged.** The
  `gatewayRequest`/`gatewayResponse` shape is unvalidated, so the handler scans
  string leaves generically and forwards anything it does not recognise. Validate
  against live gateway traces before relying on it.
- **No fail-open/fail-closed decision exists.** There is no try/except around
  `ApplyGuardrail`, so a Bedrock throttle surfaces as a Lambda failure rather than
  a defined behaviour. Decide which you want before production.

Also: the guardrail resolves to the **`DRAFT`** version (`guardrail.attr_version`).
Pin a published version for production.

---

## Memory resource policy

`enable_resource_policies=true` attaches an
`AWS::BedrockAgentCore::ResourcePolicy` to the memory resource: allow this
account's root principal, deny everything whose `aws:PrincipalOrgID` is not your
org, with `aws:ViaAWSService: false` so service-mediated access still works.

It **requires `org_id`** — the stack raises rather than deploying a policy with a
hole in it. `deploy.sh` prompts for `o-xxxx` interactively and hard-stops under
`NON_INTERACTIVE=1`.

Memory is where conversation history lives, keyed by `actor_id` = the verified
`sub` claim. That makes it the tenant boundary, which is why the federated
multi-account strategy keeps memory **per workload account**: account isolation is
the strongest wall available.

---

## Networking — read this before saying "isolated"

`enable_networking=true` creates a VPC (`10.0.0.0/16`, 2 AZs, **1 NAT gateway**),
public + `PRIVATE_WITH_EGRESS` subnets, a runtime security group, and AgentCore
VPC endpoints. Runtimes get `network_mode: VPC` with the private subnets and that
security group.

**This is not an air-gapped VPC.** Private subnets keep a NAT route to the
internet — deliberately, because AgentCore ENIs in a *public* subnet get no
internet route at all and the runtimes would lose Bedrock access. If a customer
needs no-internet-egress, that is a different design (endpoints for every
dependency, no NAT), not a flag.

What genuinely constrains the agent:

- The runtime security group has **no inbound rules** and egress limited to
  **TCP 443 only** (`allow_all_outbound=False`). Callers reach the agent through
  the AgentCore data plane, not through the VPC.
- With `enable_vpc_endpoints` (set true whenever networking is on): interface
  endpoints for Bedrock Runtime, ECR API, ECR Docker, CloudWatch Logs, the
  AgentCore Gateway endpoint, and a **gateway** endpoint for S3. The ECR + S3 ones
  matter for cost as well as isolation — AgentCore pulls and refreshes the image
  from ECR whose layers live in S3, so without them the traffic bills as NAT data
  processing. The S3 gateway endpoint is free.

### The VPC endpoint policy restricts less than it looks like

`org_id` renders `vpce.agentcore-in-org` onto the AgentCore endpoint: allow the
four data-plane actions, then deny anything whose `aws:PrincipalOrgID` is outside
your org — **but only where that key exists.** OAuth/JWT callers carry no IAM
principal, so they arrive under `Principal: "*"` with no `aws:PrincipalOrgID`, and
the deny's `Null` condition (`"aws:PrincipalOrgID": "false"`) makes it
inapplicable to them. **The org restriction covers SigV4 callers only.** JWT
callers are governed by the gateway/runtime authorizers instead.

The repo is honest about this in the policy itself — the two statement ids read
`AllowDataPlaneIncludingOAuth` and `DenySigV4CallersOutsideOrg`. Read them out loud
if someone in the room is about to call this an org boundary for all traffic.

**And it lands on one endpoint out of six.** Measured on a real
`ENABLE_NETWORKING=true ORG_ID=o-… ` deploy: only
`com.amazonaws.<region>.bedrock-agentcore.gateway` carries the org policy. The
other five — `bedrock-runtime`, `ecr.api`, `ecr.dkr`, `logs`, and the S3 gateway
endpoint — are created with the AWS **default** endpoint policy, which is
`Action: "*"`, `Principal: "*"`, `Resource: "*"`. `bedrock-runtime` is the
model-invocation path, so "we set `ORG_ID`, our endpoints are org-restricted" is
wrong about the endpoint a reviewer cares most about. Check before claiming:

```bash
VPC=$(aws ssm get-parameter --name "/$PROJECT_NAME/$ENVIRONMENT/networking/vpc-id" \
  --query Parameter.Value --output text)
aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=$VPC \
  --query 'VpcEndpoints[].[ServiceName,length(PolicyDocument)]' --output text
```

A ~128-character policy is the wide-open default; the org-restricted one is ~500.

And the failure mode that costs a control silently: **`enable_networking=true`
without `ORG_ID` does not fail.** It warns and creates the endpoint with **no
policy at all**. Treat that warning as an error. Provable for free, before any
deploy — this is the cheapest instance of the pattern taught above:

```bash
cdk synth $PREFIX-networking -c enable_networking=true | grep -c PolicyDocument
#   → 0
cdk synth $PREFIX-networking -c enable_networking=true -c org_id=o-xxxx | grep -c PolicyDocument
#   → 1
```

### The AZ trap — it is not hypothetical, and it is not a quick fix

AgentCore supports a limited set of AZ **ids** per Region
(`infra_utils/runtime_network.py:19`, `SUPPORTED_ZONE_IDS`), and AZ *name* → *id*
mapping differs per account — `us-east-1a` is not the same physical zone in two
accounts. `networking_stack.py` asks CDK for `max_azs=2`, which takes the first two
alphabetically, so whether you land in a supported zone is **an accident of which
account you are in**.

Measured on a fresh account in `us-east-1`, where the supported ids are
`use1-az1`, `use1-az2`, `use1-az4`:

| AZ name | AZ id in that account | Supported |
|---|---|---|
| `us-east-1a` | `use1-az6` | **no** |
| `us-east-1b` | `use1-az1` | yes |

CDK picked `1a` and `1b`. The networking stack reached `CREATE_COMPLETE` with one
unusable subnet, and nothing at deploy time objected — `unsupported_zone_ids()` is
called **only** by `check_network.py`, never by `app.py` or the runtime stack, which
pass `private_subnet_ids` through unfiltered (`app.py:383-386`).

**The bill comes due when a runtime tries to enter the VPC**, and AgentCore's own
message is excellent:

```
Reason: The following subnets are in unsupported availability zones in region
us-east-1: subnet-0fa4… in us-east-1a (ID: use1-az6). Supported availability
zones are: use1-az4, use1-az1, use1-az2
```

`UPDATE_FAILED` → clean `UPDATE_ROLLBACK_COMPLETE` in ~90s, so the runtime survives
in `PUBLIC` mode. Nothing is wedged; you have simply not got a VPC deployment.

**Three things make this expensive mid-session, so plan for them:**

1. **There is no flag.** `max_azs=2` is hardcoded and no context key, env var or
   `platform.yaml` entry overrides it. The fix is a **source edit** to
   `stacks/networking_stack.py` — replace `max_azs=2` with the AZ *names* that map
   to supported *ids in that account*:

   ```bash
   aws ec2 describe-availability-zones --region "$AWS_REGION" \
     --query 'AvailabilityZones[].{Name:ZoneName,Id:ZoneId}' --output table
   ```
   ```python
   availability_zones=["us-east-1b", "us-east-1c"],   # ← whichever map to supported ids
   ```

2. **You cannot apply it in place.** Changing AZs forces subnet replacement, and
   CloudFormation creates the replacements before deleting the originals — so they
   collide on their own CIDRs and the whole update rolls back:
   `The CIDR '10.0.2.0/24' conflicts with another subnet … HandlerErrorCode: AlreadyExists`.
   `check_network.py`'s advice to "redeploy the networking stack" is not enough.
   **Destroy the stack, then deploy it.** Measured: destroy 4m32s, redeploy 3m32s.

3. **Then the runtimes still need redeploying** — see below.

Budget roughly **15 minutes** end to end from "the verify went red" to a full
`PASS`, and say so rather than debugging live. Because it is account-dependent, run
`check_network.py` in the target account *before* the session if you can.

### Module C does not put your agents in the VPC

Deploying the networking stack gives you a VPC, subnets, a NAT gateway and six
endpoints that **nothing is using**. Existing runtimes keep `networkMode: PUBLIC`
until they are redeployed with `enable_networking=true`. Measured: with all three
runtimes already deployed and the networking stack `CREATE_COMPLETE`,
`check_network.py --expect-public` **passed** — the agents were still public.

```bash
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module 6   # orchestrator
ENABLE_NETWORKING=true ORG_ID=o-xxxx ./scripts/deploy.sh deploy --module 8   # A2A sub-agents
```

Measured with no source change (so no container rebuild): module 6 in 345s, module
8 in 172s.

**`check_network.py` reports one problem at a time**, so this arrives as a second
red after you have fixed the first. The real sequence on a fresh account is: AZ
failure → fix and recreate the stack → *placement* failure → redeploy the runtimes →
`PASS`. Knowing there are two stages is the difference between one detour and two.

### Confirming the posture for real

```bash
.venv/bin/python scripts/check_network.py                   # AZ ids, then runtime placement
.venv/bin/python scripts/check_network.py --expect-public    # assert a non-VPC deploy is public on purpose
aws ec2 describe-network-interfaces \
  --filters Name=interface-type,Values=agentic_ai \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,AvailabilityZone,SubnetId,Status]' --output text
```

`--expect-public` is the underrated one: it lets you prove a non-VPC deployment is
public **deliberately** rather than accidentally. It is a real assertion in both
directions — measured returning exit 1 with
`FAIL: code-agent: expected networkMode PUBLIC, got VPC` once the runtimes moved.

The ENI check is the evidence a compliance reviewer actually wants, and it shows
something the stack outputs do not: **AgentCore creates fewer ENIs than you have
runtimes.** Three runtimes in the VPC produced **two** `agentic_ai` ENIs, one per
private subnet, shared across runtimes. Do not treat "one ENI per agent" as the
expected shape.

Then prove the isolation did not break anything, which is the question that
actually gets asked:

```bash
.venv/bin/python scripts/invoke.py "Reply with exactly: VPC OK"
.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A VPC OK"
```

Both measured working in VPC mode in under 10s — Bedrock over the interface
endpoint, everything else over NAT.

---

## Module E — what KMS + CloudTrail actually gives you

`$PREFIX-security` creates:

- a **KMS CMK** with alias `alias/$PREFIX-agentcore` and key rotation enabled.
  `app.py` passes its ARN to the memory stack as `encryption_key_arn`, so memory
  is CMK-encrypted when module E is deployed — this is the one place the key is
  actually consumed.
- a **CloudTrail** trail `$PREFIX-agentcore-trail` into `$PREFIX-cloudtrail-<account>`.

Three honest caveats:

- The trail is **`is_multi_region_trail=False`** — activity in other Regions is not
  captured. Most orgs already run an org trail; do not present this as replacing it.
- The bucket is **S3-managed encryption**, not the CMK you just created, and has
  `auto_delete_objects=True` with `RemovalPolicy.DESTROY`. Destroying the stack
  deletes the audit log. That is right for a workshop and wrong for production.
- The CMK also has `RemovalPolicy.DESTROY`.

**`deploy --module E` on its own fails** with `No stacks match the name(s)
…-security`, because `enable_security` defaults false and module E does not set its
own flag. Use `ENABLE_SECURITY=true ./scripts/deploy.sh deploy --module E`. See
`troubleshooting.md`.

What the trail actually records, verified on a real deploy — this is the answer to
"so what is being audited?", and it is worth having ready:

| Setting | Value |
|---|---|
| `IncludeManagementEvents` | `true`, `ReadWriteType: All` |
| `DataResources` | **none** — no S3/Lambda data-plane events |
| `IncludeGlobalServiceEvents` | `true` |
| `IsMultiRegionTrail` | **`false`** — hardcoded, no flag |
| `IsOrganizationTrail` | `false` |
| `LogFileValidationEnabled` | `true` |
| `KmsKeyId` | **`null`** — log files are SSE-S3, not CMK-encrypted |

So it is a single-Region management-event trail with integrity validation, in a
bucket that deletes itself on teardown. That is a reasonable workshop artifact and
not an audit posture. Check it yourself rather than trusting the stack status:

```bash
TRAIL=$PREFIX-agentcore-trail
aws cloudtrail get-trail --name "$TRAIL" \
  --query 'Trail.{Multi:IsMultiRegionTrail,KMS:KmsKeyId,Org:IsOrganizationTrail,Validation:LogFileValidationEnabled}'
aws cloudtrail get-event-selectors --trail-name "$TRAIL"
```

Module E is the prerequisite for `enable_traceability` (module 9's SNS +
EventBridge alerting on sensitive AgentCore API calls), because that rule only
fires when CloudTrail management events are being recorded. Two follow-ups nobody
remembers: **subscribe an endpoint to the SNS topic** or the alerts go nowhere,
and the topic is **not KMS-encrypted** — add a CMK if alert contents are sensitive.

---

## Org guardrails (Terraform) — the enterprise conversation

`terraform/org-guardrails/` attaches the org-scope SCPs. Run it from the
**Organizations management account or a delegated SCP administrator**, with SCPs
enabled for the org (`aws organizations enable-policy-type`).

```hcl
module "agentcore_org_guardrails" {
  source      = "../../terraform/org-guardrails"
  name_prefix = "agentcore"
  target_ids  = ["ou-abcd-1234wxyz"]
}
```

```bash
cd terraform/org-guardrails
terraform init
terraform apply -var 'target_ids=["ou-..."]'
```

**Attach to a sandbox OU first.** SCPs are additive-deny and layer on
`FullAWSAccess`, so attaching is non-destructive to existing permissions — but a
gateway hardening SCP that requires a Cedar policy engine in `ENFORCE` will block
gateway creation for every team in that OU, including the ones who never asked for
it. `target_ids` entries are validated at plan time (`r-…`, `ou-…-…`, or a 12-digit
account id) so a typo fails fast.

### The gateway SCPs deploy as one policy, not eight

Organizations allows **5 SCPs per target**, and `FullAWSAccess` already holds one —
4 usable. Nine individual policies would not fit any real target, so `gateway.tf`
merges the `Statement` arrays of all eight gateway documents into a single
consolidated SCP, `${name_prefix}-scp-gateway-guardrails`. SCPs are additive-deny,
so merging is semantically identical to attaching separately.

Consequences:

- `enable_gateway_scps` is **all-or-nothing**; subsetting means editing the map in
  `gateway.tf`.
- Plan-time preconditions enforce the **5,120-character** SCP size quota and Sid
  uniqueness across library files. A ninth control can fail at plan time.
- With everything on the module attaches **3** SCPs per target (consolidated
  gateway + memory + identity), leaving one usable slot. A fourth standalone SCP is
  the last one that fits.

Both `enable_scp_memory_enforce_cmk` and `enable_gateway_scps` default to `true`,
so a bare `terraform apply` attaches them unless you opt out.

### What the gateway SCPs constrain

| SCP | Condition key | Effect |
|---|---|---|
| `require-cmk` | `KmsKeyArn` | gateway must use an approved CMK |
| `deny-no-auth` | `GatewayAuthorizerType` | blocks `NONE` — no unauthenticated gateways |
| `require-policy-engine` | `PolicyEngineArn` / `PolicyEngineMode` | requires a Cedar engine in `ENFORCE` |
| `enforce-approved-idp` | `DiscoveryUrl` | JWT gateways must use an approved IdP |
| `restrict-protocol` | `ProtocolType` | restricts explicitly-set protocols to `MCP` |
| `targets-require-private-endpoint` | `PrivateEndpointType` | targets must use a private endpoint |
| `targets-restrict-credential-provider` | `CredentialProviderType` | denies `API_KEY` / `JWT_PASSTHROUGH` |
| `targets-restrict-type` | `McpTargetConfigurationType` | allow-lists target types (lambda, mcpServer) |

**These are control-plane controls: they constrain how a gateway may be
*configured*, not who may *invoke* one.** That distinction is the whole point of
the section. `restrict-protocol` has a matching gap worth disclosing — an
*omitted* protocol falls back to the service default and is unconstrained; only
explicitly-set values are checked.

For invoke-time restrictions, the mechanism this repo uses is a per-resource
policy attached with `AWS::BedrockAgentCore::ResourcePolicy`, as done for Memory.

The **"fully-private gateway" bundle** worth naming as a package:
`deny-no-auth` + `require-policy-engine` + `targets-require-private-endpoint`.

### The identity SCP is the sharpest control here

`scp.identity.deny-workload-token-for-userid` denies
`bedrock-agentcore:GetWorkloadAccessTokenForUserId`. **That API takes the user
identifier as an unverified string** — any principal holding the action can mint a
workload access token for any user and read that user's stored credentials out of
the token vault, with no JWT and no proof of identity anywhere in the call.

Agents behind Runtime or Gateway inbound auth never need it: the caller's verified
token arrives with the request, and `GetWorkloadAccessTokenForJWT` is the path that
checks it.

The exemption parameter defaults to a role ARN **that cannot exist**, so the
control denies everyone until an operator supplies a real pattern. Narrow it only
for a genuine break-glass or migration path:

```bash
terraform apply -var 'target_ids=["ou-..."]' \
  -var 'identity_approved_principal_arn_pattern=arn:aws:iam::111122223333:role/break-glass'
```

The action also supports the `bedrock-agentcore:userid` condition key if you need
something narrower than an ARN exemption. Prefer removing the need over widening
the pattern.

---

## Runtime IAM — what the agent's role can actually do

This is what a compromised agent inherits, so read it rather than assuming.
`stacks/runtime_stack.py` builds the role with scoped statements: ECR pull limited
to **this component's own repository**, Logs limited to
`/aws/bedrock-agentcore/runtimes/*`, SSM limited to
`arn:aws:ssm:*:*:parameter/{project}/*`, Bedrock limited to `foundation-model/*`
plus `inference-profile/*`, and the token-vault secret read scoped to
`bedrock-agentcore-identity!default/oauth2/*`.

Wildcards remain in three places, two of them unavoidable:

| Statement | Resource | Why |
|---|---|---|
| `ecr:GetAuthorizationToken` | `*` | returns an account-level token; IAM accepts nothing else |
| `xray:PutTraceSegments` / `PutTelemetryRecords`, `cloudwatch:PutMetricData` | `*` | these actions take no resource ARN (`PutMetricData` is constrained by a `cloudwatch:namespace` condition instead) |
| `bedrock-agentcore:*` — gateway invoke, memory data plane, code interpreter, browser, workload tokens | `*` | **knowingly open.** Scoping needs gateway, memory and sibling-runtime ARNs, and the A2A targets do not exist when the role is built. Say this out loud in a review rather than letting someone find it. |

Two related facts that explain real failures:

- The memory data-plane actions (`ListEvents`, `CreateEvent`, `DeleteEvent`,
  `ListSessions`, `RetrieveMemoryRecords`) are there because framework
  integrations need them — LangGraph's checkpointer lists events to rehydrate a
  thread. Remove them and memory-backed patterns fail at invoke while the stack
  looks fine.
- `secretsmanager:GetSecretValue` on the vault path is what makes gateway tools
  load. Without it the token fetch raises AccessDenied inside the MCP client and
  the agent aborts. See `agent-patterns.md`.

**The credential-provider fence is IAM and nothing else.** AgentCore does not
enforce any binding between a workload identity and the credential providers it may
read, so a shared execution role hands every agent every provider's credentials.
One workload identity and one role per trust boundary, each naming exactly one
provider — that is what `iam.identity-credential-provider-scoped` is a template for.

One trap when adopting it: `GetResourceOauth2Token` and the
`GetWorkloadAccessToken*` actions declare several **required** resource types — the
directory and the token vault as well as the workload identity and the provider —
so a statement naming only the provider ARN authorises nothing. The resulting
`AccessDenied` is easy to "fix" by widening `Resource` to `"*"`, which defeats the
control entirely. `Deny` statements need only the ARN they target.

---

## The Cognito M2M client secret is a plaintext CloudFormation export

Find this before a reviewer does, because it is the one finding in this platform
that a reviewer can confirm in a single read-only API call.

The `$PREFIX-auth` stack publishes the Cognito **M2M app client secret in
plaintext** as an auto-generated cross-stack output *and* a named CloudFormation
export:

```bash
aws cloudformation describe-stacks --stack-name "$PREFIX-auth" \
  --query 'Stacks[0].Outputs[?contains(OutputKey,`ClientSecret`)].OutputKey'
# ExportsOutputFnGetAttUserPoolM2MClientDescribeCognitoUserPoolClient…ClientSecret…

aws cloudformation list-exports \
  --query 'Exports[?contains(Name,`ClientSecret`)].Name'
# the same value, retrievable WITHOUT knowing the stack name
```

**This is not a misconfiguration you can flag off.** It falls out of the wiring:
`stacks/auth_stack.py` exposes the secret as a property, `app.py` passes it to a
*different* stack, and `stacks/identity_stack.py` calls `.unsafe_unwrap()` on it.
A cross-stack reference becomes a CloudFormation export, and an export carries the
**resolved value**, not the token. `list-imports` on that export names
`$PREFIX-identity`, which is the whole chain in one call.

The property's own docstring says the value "renders as a CloudFormation token …
never literal text." That is **true of the synthesized template and false of the
deployed stack.** Do not quote the docstring as reassurance; check the outputs.

Why it is worse than it sounds: reading it needs `cloudformation:DescribeStacks`
or `cloudformation:ListExports` — permissions handed to anyone who looks at
infrastructure, and `ListExports` accepts no resource condition, so it cannot be
scoped to one stack. The equivalent Secrets Manager read is a deliberate grant
that CloudTrail records as a secret access.

The blast radius is bounded but real: that secret plus the `m2m-client-id` in SSM
is a complete client-credentials grant for the `agentcore/invoke` scope — enough
to call the gateway directly as a machine principal, bypassing whatever the agent
would have done. It is **not** a path to the user pool's human identities.

What actually mitigates it, in order of how quickly it can be done:

1. **Treat the `-auth` stack as secret-bearing** in whatever governs who may
   describe stacks. This costs nothing and is the honest short answer.
2. **Rotate after any session where the outputs were on screen** — delete and
   recreate the M2M client, or redeploy `-auth` into a fresh prefix.
3. **Break the cross-stack reference** if you are forking: have `-identity` read
   the secret from Secrets Manager at deploy time (`{{resolve:secretsmanager:…}}`,
   the pattern the accelerator already uses for the IdP secret) instead of
   receiving it through `unsafe_unwrap()`. Removing the reference removes the
   export.

There is a third path, and it is the one that catches people: **the deploy prints
it.** Two separate mechanisms, and the second is much wider than the first:

| Where | Scope | Why |
|---|---|---|
| CDK's `Outputs:` block | runs whose stack graph reaches `-auth` | includes runs that never name it — `deploy --module A` announces `Stacks: …-memory`, then prints `-auth`'s outputs because CDK pulls it in as a dependency |
| `deploy.sh`'s closing summary table | **every `deploy` run, once `-auth` exists** | `print_summary` (`scripts/deploy.sh:659`) does its own `list-stacks` for `starts_with(StackName, '$PREFIX')` and dumps all outputs of every match — it never looks at what you deployed |

So the correct rule is the blunt one: **once module 3 has ever run in this account,
every subsequent `deploy.sh deploy` prints the secret.** Verified with
`deploy --module C`, whose graph does not touch `-auth` at all: CDK's own `Outputs:`
block listed only `-networking`, and the closing summary printed the `-auth` secret
anyway.

`./scripts/deploy.sh export` reads the same outputs and **would** write the secret
to `workshop-outputs-<stamp>.json` in the repo root, which is **not** covered by
`.gitignore`. Today it does not, and the reason is narrow enough to be worth stating
precisely: the export's merge only keeps the **last field of each output object**
(see `troubleshooting.md`), and because the secret output is a CloudFormation
*export*, its last field is `ExportName` rather than `OutputValue`. So the file gets
`…-auth:ExportsOutputFnGetAtt…UserPoolClientClientSecret…` — the export's name, not
its value. Verified on a full platform-team export: the longest lowercase-alphanumeric
run in the file is 36 characters, and a Cognito client secret is ~52.

That is a bug shielding a leak, not a safeguard, and it is one refactor away from
inverting. If you fix the export locally, gitignore the artifact in the same change.
The file still carries the account id and every runtime, memory and VPC ARN, so it is
not shareable either way.

Checked and **not** a leak, so do not claim it: the auth stack's custom-resource
provider Lambda does not log the response. `filter-log-events` on its log group
for `ClientSecret` / `secret` returns nothing. The exposure is the two
CloudFormation APIs above plus the deploy's own stdout — see `facilitation.md`
about screens and recordings.

---

## Proving a control is real without deploying

`cdk synth` is the cheap answer to "prove this control exists." Flag off → the
resource count is 0; flag on → the resource is present. `docs/TESTING.md` in the
repo has ready-made synth assertions per control, which is a much better response
in a security conversation than a screenshot of a green stack.

```bash
cdk synth agentcore-workshop-dev-gateway -c enable_cedar=true  | grep -c PolicyEngine
cdk synth agentcore-workshop-dev-gateway                        | grep -c PolicyEngine
```

---

## Never do these

- **`cedar_mode=ENFORCE` before reading LOG_ONLY decision logs.** The shipped
  permit covers one tool. Everything else stops.
- **Attach the org SCPs outside a sandbox OU on first run.** They constrain every
  account under the target, including teams who did not ask.
- **Call `enable_networking=true` air-gapped.** There is a NAT route, by design.
- **Deploy `enable_networking=true` without `ORG_ID`** and assume the endpoint is
  org-restricted. It has no policy at all, and only warns.
- **Say `ORG_ID` org-restricts your endpoints.** It restricts one of six. The other
  five, including `bedrock-runtime`, keep the wide-open AWS default policy.
- **Say the agents are in the VPC because module C succeeded.** Runtimes stay
  `PUBLIC` until redeployed with `enable_networking=true`. Prove it with
  `check_network.py` plus the `agentic_ai` ENI list, and note that
  `--expect-public` will happily pass in exactly this half-done state.
- **Present `iam.*` library files as deployed controls.** They are reference
  policies nothing applies.
- **Put a secret in `platform.yaml`, `workshop.env`, or CDK context.** Secrets
  Manager *names* only. Context ends up in `cdk.context.json` and CloudFormation
  parameters.
- **Say the Cognito M2M secret only lives in Secrets Manager.** The `-auth` stack
  exports it in plaintext, readable with `cloudformation:ListExports`. Section
  above.
- **Rely on the egress guardrail's `DRAFT` version in production.** Pin a
  published version.
