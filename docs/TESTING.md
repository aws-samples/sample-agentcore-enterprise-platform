# Testing the AgentCore security controls

How to test the security controls in this repo:

- `control-library/` — IaC-agnostic control definitions + `catalog.yaml`
- `infra_utils/policy_loader.py` — loads/parameterizes controls
- **Item 1 (SCP):** CMK-for-Memory SCP applied via `terraform/org-guardrails/`
- **Item 4 (resource policy):** Memory in-account-only resource policy (`enable_resource_policies`)
- **Items 5+6 (Guardrails + interceptor):** egress Lambda interceptor + Bedrock Guardrail
  on the Gateway (`enable_egress_filter`)
- **Item 3 (Cedar):** AgentCore policy engine with an explicit read permit on the
  Gateway; Cedar's implicit default-deny covers everything else
  (`enable_cedar`, `cedar_mode` LOG_ONLY/ENFORCE)
- **Item 2 (VPCE + IAM):** fine-grained AgentCore VPC endpoint policy + tightened execution
  role (`enable_networking`, `org_id`)
- **Item 7 (observability):** SNS + EventBridge alerting on sensitive AgentCore API calls
  (`enable_traceability`)

There are two layers of testing:

- **Part A — Local (no AWS account needed):** loader, validator, unit tests, `cdk synth`,
  `terraform validate`. This is what CI runs and is enough to review a PR.
- **Part B — Live (needs an AWS account):** deploy and exercise the controls for real.

---

## Prerequisites

```bash
cd /path/to/agentcore-accelerator

# Python venv (repo targets 3.13; 3.12 also works locally)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Node + CDK + Terraform
node --version        # v18+
cdk --version         # 2.2xx
terraform version     # 1.3+
```

---

## Part A — Local tests (no AWS account)

### A1. control-library loader + catalog

```bash
source .venv/bin/activate
python - <<'PY'
from infra_utils.policy_loader import load_control, load_catalog
print("controls:", [c["id"] for c in load_catalog()["controls"]])
# SCP renders with its default param
print(load_control("scp.memory.enforce-cmk")["Statement"][0]["Condition"])
# Resource policy renders with injected params
print(load_control("resource-policy.memory.in-account-only", {
    "account_id": "111122223333",
    "memory_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/mem-abc",
    "org_id": "o-example",
})["Statement"][1]["Condition"])
PY
```

> If a heredoc misbehaves in your shell, drop the snippet into a temp `.py` file and run it.

### A2. Validate the control-library against its catalog

```bash
make validate-controls
# or: python scripts/validate_control_library.py
```

Checks every control file exists, is valid JSON, has matching `<<sentinel>>`/param
declarations, and renders with no unresolved tokens. Installs of `checkov` add a deep policy
scan; without it you get a single warning (non-fatal).

### A3. Unit tests

```bash
make test-controls
# or: python -m pytest tests/ -q      → 12 passed
```

### A4. CMK-for-Memory SCP via Terraform (item 1)

```bash
cd terraform/org-guardrails
terraform fmt -check -recursive .
terraform init -backend=false
terraform validate                    # → Success! The configuration is valid.

# Confirm the SCP renders from the shared control-library file (no leftover <<sentinel>>):
printf 'local.cmk_scp_rendered\n' | terraform console
cd ../..
```

Expect the printed policy to contain `"bedrock-agentcore:CreateMemory"` and the resolved
`arn:aws:kms:*:*:key/*` pattern (or your override), and **no** `<<kms_key_arn_pattern>>`.

### A4b. Gateway configuration SCPs render (control-plane, item "1b")

The 8 gateway controls are rendered individually from the control-library
(`local.gateway_scp_rendered`) and then merged into ONE consolidated SCP
(`local.gateway_scp_consolidated`) to fit the 5-SCPs-per-target Organizations quota.

```bash
cd terraform/org-guardrails
terraform init -backend=false
terraform validate

# All 8 gateway controls are rendered from the library:
printf 'keys(local.gateway_scp_rendered)\n' | terraform console                                            # → the 8 control names
# Sentinels resolved in the per-control renders:
printf 'local.gateway_scp_rendered["enforce-approved-idp"]\n' | terraform console | grep -c 'https://'     # → 1 (DiscoveryUrl pattern injected)
printf 'local.gateway_scp_rendered["require-cmk"]\n' | terraform console | grep -c 'kms_key_arn_pattern'   # → 0 (sentinel resolved; note console wraps output in <<EOT)

# The consolidated document contains every control's statements, no unresolved sentinels,
# and stays under the 5,120-character SCP quota:
printf 'local.gateway_scp_statements[*].Sid\n' | terraform console                                         # → 10 unique Sids (require-cmk and require-policy-engine carry 2 each)
printf 'local.gateway_scp_consolidated\n' | terraform console | grep -c '<<'                               # → 0 (no unresolved <<sentinel>> tokens)
printf 'length(local.gateway_scp_consolidated)\n' | terraform console                                      # → < 5120
cd ../..
```

These SCPs use the launched **control-plane** condition keys only. Data-plane ingress
controls (`aws:SourceVpc`, `InboundJwtClaim/*`) and RCP support are on the roadmap in
`SECURITY_CONTROLS.md` and are not shipped yet.

### A4c. Identity SCP denies the unverified-userId token path

`GetWorkloadAccessTokenForUserId` accepts the user identifier as an unverified string, so the
shipped default must exempt nobody. The exemption param defaults to a role ARN that cannot
exist, which makes the control a blanket deny until an operator supplies a real pattern.

```bash
cd terraform/org-guardrails
terraform init -backend=false
terraform validate

VARS='-var target_ids=["ou-example-11111111"]'

# Sentinel resolved, and the statement denies the right action:
printf 'length(regexall("<<", local.identity_userid_scp_rendered))\n' | terraform console $VARS   # → 0
printf 'jsondecode(local.identity_userid_scp_rendered).Statement[0]\n' | terraform console $VARS  # → Deny on GetWorkloadAccessTokenForUserId

# The default exemption names a role that does not exist, so nothing is exempt:
printf 'jsondecode(local.identity_userid_scp_rendered).Statement[0].Condition.ArnNotLike["aws:PrincipalArn"]\n' \
  | terraform console $VARS                                                                      # → arn:aws:iam::*:role/__no_principal_may_mint_tokens_by_userid__

# Sid must not collide with the consolidated gateway SCP's statements:
printf 'contains(local.gateway_scp_statements[*].Sid, "DenyWorkloadTokenForUserId")\n' \
  | terraform console $VARS                                                                      # → false

# Quota: 1 memory + 1 consolidated gateway + 1 identity = 3 of the 4 usable slots per target
# (5-per-target limit minus FullAWSAccess). This mirrors the attachments_per_target output.
printf '(var.enable_scp_memory_enforce_cmk ? 1 : 0) + (var.enable_gateway_scps ? 1 : 0) + (var.enable_scp_identity_deny_token_for_userid ? 1 : 0)\n' \
  | terraform console $VARS                                                                      # → 3
cd ../..
```

The matching `iam.identity-credential-provider-scoped` reference policy is covered by the
loader tests in A3: it asserts the workload-identity and OAuth2-provider ARNs carry no
wildcard, since AgentCore does not bind a workload identity to the providers it may read and
IAM is the only thing scoping that access.

### A5. Memory resource policy synthesizes only when enabled (item 4)

The Memory resource policy is attached with the native `AWS::BedrockAgentCore::ResourcePolicy`.

```bash
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1

# Flag OFF → no resource policy
rm -rf cdk.out
cdk synth agentcore-workshop-dev-memory 2>/dev/null \
  | grep -c 'AWS::BedrockAgentCore::ResourcePolicy'                                   # → 0

# Flag ON → one native ResourcePolicy with the rendered in-account-only policy
rm -rf cdk.out
cdk synth agentcore-workshop-dev-memory \
  -c enable_resource_policies=true -c org_id=o-example123 2>/dev/null \
  | grep -oE 'AWS::BedrockAgentCore::ResourcePolicy|o-example123|PrincipalOrgID' | sort | uniq -c
# → 1 each (resource policy present, org_id + PrincipalOrgID deny guard rendered)

# Flag ON but missing org_id → clean failure
rm -rf cdk.out
cdk synth agentcore-workshop-dev-memory -c enable_resource_policies=true 2>&1 \
  | grep 'requires org_id'
```

### A6. Egress interceptor + Guardrail synthesize only when enabled (items 5+6)

```bash
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1

# Flag ON → Guardrail + interceptor Lambda + interceptor config
rm -rf cdk.out
cdk synth agentcore-workshop-dev-gateway -c enable_egress_filter=true 2>/dev/null \
  | grep -cE 'AWS::Bedrock::Guardrail|egress-interceptor|InterceptorConfigurations|ApplyGuardrail'
# → 4 (one match per pattern)

# Flag OFF → none of the above
rm -rf cdk.out
cdk synth agentcore-workshop-dev-gateway 2>/dev/null \
  | grep -c 'AWS::Bedrock::Guardrail'                                                  # → 0
```

### A7. Cedar policy engine synthesizes only when enabled (item 3)

```bash
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1

# Flag ON → policy engine + LOG_ONLY policy-engine config on the gateway. Cedar is
# implicit default-deny, so no blanket forbid ships (a matching forbid would override
# every permit — ENFORCE would then deny all tool calls, including the permitted reads).
rm -rf cdk.out
cdk synth agentcore-workshop-dev-gateway -c enable_cedar=true 2>/dev/null \
  | grep -cE 'AWS::BedrockAgentCore::PolicyEngine|PolicyEngineConfiguration'
# → 2 (engine and the gateway's policy-engine config)

# Exactly one Cedar policy resource — the read permit — and no forbid statement:
rm -rf cdk.out
cdk synth agentcore-workshop-dev-gateway -c enable_cedar=true 2>/dev/null > /tmp/cedar-synth.yaml
grep -c 'Type: AWS::BedrockAgentCore::Policy$' /tmp/cedar-synth.yaml   # → 1 (the read permit)
grep -c 'forbid(principal' /tmp/cedar-synth.yaml                       # → 0 (implicit default-deny)
rm -f /tmp/cedar-synth.yaml

# Try enforce mode
rm -rf cdk.out
cdk synth agentcore-workshop-dev-gateway -c enable_cedar=true -c cedar_mode=ENFORCE 2>/dev/null \
  | grep -c 'ENFORCE'                                                                  # → 1

# Flag OFF → no policy engine config
rm -rf cdk.out
cdk synth agentcore-workshop-dev-gateway 2>/dev/null | grep -c 'PolicyEngineConfiguration'  # → 0
```

### A7b. VPC endpoint policy synthesizes with the SigV4-org-scoped policy (item 2)

The endpoint policy allows the AgentCore data-plane actions (invoke + OAuth PRM discovery)
with `Principal: "*"` and **no** IAM-principal conditions — OAuth/JWT callers carry no IAM
principal, so any principal-keyed condition would block them (per AWS docs). The org
restriction is a Deny scoped by a `Null` guard so it only fires for SigV4 callers outside
the org, never for principal-less OAuth traffic.

The networking VPC needs Availability Zone context to synth offline. Provide it once with a
temporary `cdk.context.json` (git-ignored), then synth:

```bash
source .venv/bin/activate
cat > cdk.context.json <<'JSON'
{ "availability-zones:account=111122223333:region=us-east-1": ["us-east-1a","us-east-1b"] }
JSON

rm -rf cdk.out
CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1 \
  cdk synth agentcore-workshop-dev-networking -c enable_networking=true -c org_id=o-example123 2>/dev/null \
  > /tmp/vpce-synth.yaml

grep -c 'bedrock-agentcore.gateway' /tmp/vpce-synth.yaml            # → 1 (the AgentCore gateway endpoint service)
grep -c 'PrincipalOrgID' /tmp/vpce-synth.yaml                       # → 2 (org condition + Null guard, both inside the SigV4-only Deny)
grep -c 'GetRuntimeProtectedResourceMetadata' /tmp/vpce-synth.yaml  # → 1 (OAuth PRM discovery allowed with Principal "*")
grep -c '"Null":' /tmp/vpce-synth.yaml                              # → 1 (Null guard keeps the Deny off principal-less OAuth traffic)

rm -f cdk.context.json /tmp/vpce-synth.yaml && rm -rf cdk.out
```

Also confirm the tightened execution role scopes SSM to the project path (no `"*"`):

```bash
grep -A2 '"ssm:GetParameter"' -r infra_utils/agentcore_role.py
# resources = arn:aws:ssm:*:*:parameter/{project_name}/*
```

### A7c. Observability alerting synthesizes only when enabled (item 7)

```bash
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1

# Flag ON → SNS topic + EventBridge rule on sensitive AgentCore events
rm -rf cdk.out
cdk synth agentcore-workshop-dev-observability -c enable_traceability=true 2>/dev/null \
  | grep -cE 'AWS::SNS::Topic|AWS::Events::Rule'          # → 3 (topic, topic policy, rule)

# Flag OFF → none
rm -rf cdk.out
cdk synth agentcore-workshop-dev-observability 2>/dev/null | grep -c 'AWS::Events::Rule'  # → 0
```

### A8. Lint (matches CI)

```bash
source .venv/bin/activate

# Lint the new/changed security-controls files (all clean):
ruff check infra_utils/policy_loader.py scripts/validate_control_library.py \
  stacks/gateway_stack.py stacks/memory_stack.py stacks/networking_stack.py \
  stacks/observability_stack.py infra_utils/agentcore_role.py tests/

# New security-controls files are also ruff-format clean:
ruff format --check infra_utils/policy_loader.py scripts/validate_control_library.py \
  tests/ tools/egress_interceptor/handler.py
```

> Note: repo-wide `ruff check .` reports pre-existing findings in older files; the lint
> backlog is tracked separately. CI lints the files changed in each merge request.

---

## Part B — Live tests (AWS account required)

> High-level, since these touch real resources. Use a non-production account. The SCP test
> additionally requires the **Organizations management account** (or delegated admin).

### B1. Deploy the account/workload controls

```bash
source .venv/bin/activate
export ORG_ID=o-yourorgid           # required when resource policies are on

# Everything security-focused (networking + security + resource policies + egress filter):
./scripts/deploy.sh deploy --profile security-focused

# Or a single stack:
NON_INTERACTIVE=1 cdk deploy agentcore-workshop-dev-gateway -c enable_egress_filter=true
```

### B2. Verify the Memory resource policy (item 4)

```bash
MEMORY_ARN=$(aws ssm get-parameter --name /agentcore-workshop/dev/memory/memory-arn \
  --query Parameter.Value --output text)
aws bedrock-agentcore-control get-resource-policy --resource-arn "$MEMORY_ARN"
# Expect the in-account-only Allow + the aws:PrincipalOrgID Deny guard.
```

### B3. Verify the Guardrail + interceptor (items 5+6)

```bash
aws bedrock list-guardrails | grep egress-guardrail
aws lambda get-function --function-name agentcore-workshop-dev-egress-interceptor \
  --query 'Configuration.Environment.Variables'
```

Then invoke the gateway tool through the agent with:
- a prompt-injection style payload → expect the REQUEST interceptor to block (CloudWatch logs
  of the interceptor show `blocked (REQUEST)`);
- content containing an email / SSN → expect PII to be anonymized/blocked in the tool
  input/output.

Interceptor logs: `/aws/lambda/agentcore-workshop-dev-egress-interceptor`.

### B3b. Verify the Cedar policy engine (item 3)

```bash
# The policy engine is attached to the gateway; deploy with enable_cedar=true (or the
# security-focused profile). Start in LOG_ONLY so nothing is blocked while you validate.
NON_INTERACTIVE=1 cdk deploy agentcore-workshop-dev-gateway \
  -c enable_cedar=true -c cedar_mode=LOG_ONLY
```

Invoke a permitted (read) tool and a non-permitted (write) tool through the agent, then check
the policy decision logs. Only after confirming the expected allow/deny decisions, redeploy
with `-c cedar_mode=ENFORCE` to actively block.

### B4. Verify the CMK SCP (item 1) — management account

```bash
cd terraform/org-guardrails
terraform apply -var 'target_ids=["ou-abcd-1234wxyz"]'
```

Then, from a member account under that OU, attempt `CreateMemory` **without** a KMS key —
expect an explicit `AccessDenied` from the SCP. Creating with a CMK should succeed.

### B4b. Verify the VPC endpoint policy + IAM (item 2)

```bash
# Endpoint policy is attached to the AgentCore gateway interface endpoint:
aws ec2 describe-vpc-endpoints \
  --filters Name=service-name,Values="com.amazonaws.us-east-1.bedrock-agentcore.gateway" \
  --query 'VpcEndpoints[0].PolicyDocument'

# Execution role SSM scope (should be the project path, not "*"):
aws iam get-role-policy --role-name agentcore-workshop-dev-orchestrator-role \
  --policy-name <inline-policy-name>
```

### B4c. Verify observability alerting (item 7)

```bash
aws sns list-topics | grep agentcore-security-alerts
aws events list-rules --name-prefix agentcore-workshop-dev-agentcore-sensitive
# Subscribe an email/SNS endpoint, then trigger a sensitive event (e.g. PutResourcePolicy or
# DeleteGateway in a test resource) and confirm the alert fires. Requires CloudTrail
# management events (enabled by the security stack / enable_security).
```

### B5. Cleanup

```bash
./scripts/deploy.sh destroy --profile security-focused
cd terraform/org-guardrails && terraform destroy && cd ../..
```

---

## Part C — Agent pattern matrix (live)

Every pattern shares `agent-code/` but builds its own Dockerfile, so a green deploy of one
proves nothing about the others. Run this matrix before a release: it has caught defects that
a single orchestrator invoke cannot reach (a stale container image, a missing forwarded header,
and two missing runtime-role permissions — none of which fail at synth or deploy time).

One redeploy of the orchestrator runtime per pattern, roughly 5–8 minutes each (CodeBuild):

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1

for p in orchestrator strands-agent langgraph-agent claude-sdk-agent claude-sdk-multi-agent; do
  AGENT_PATTERN=$p NON_INTERACTIVE=1 ./scripts/deploy.sh deploy \
    --stack agentcore-workshop-dev-runtime-orchestrator
  python scripts/invoke.py "Reply with exactly: $p LIVE"
done

# The agui-* patterns speak AG-UI, not plain HTTP — use --agui
for p in agui-strands-agent agui-langgraph-agent; do
  AGENT_PATTERN=$p NON_INTERACTIVE=1 ./scripts/deploy.sh deploy \
    --stack agentcore-workshop-dev-runtime-orchestrator
  python scripts/invoke.py --agui "Reply with exactly: $p LIVE"
done
```

Then check the tools actually loaded, which exercises the gateway MCP client and the
AgentCore Identity token vault (`--agui` for the AG-UI patterns):

```bash
python scripts/invoke.py "List the names of the tools you have available. Names only."
# Expect the gateway target plus execute_python_securely, e.g.
#   sample-tool___text_analysis_tool, execute_python_securely
```

Two things worth asserting beyond "it answered":

```bash
# The runtime really is on the pattern you asked for (protocol + fresh image)
ARN=$(aws ssm get-parameter --name /agentcore-workshop/dev/runtimes/orchestrator/arn \
  --query Parameter.Value --output text)
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${ARN##*/}" \
  --query '[agentRuntimeVersion,protocolConfiguration,agentRuntimeArtifact]'
# agui-* patterns → serverProtocol AGUI; all others → HTTP

# Each pattern pushed its own image tag (identical tags mean no rebuild happened)
aws ecr describe-images --repository-name agentcore-workshop-dev-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-5:].{tags:imageTags,pushed:imagePushedAt}'
```

Results as of the last full run (account-agnostic; re-run per release):

| Pattern | Protocol | Invoke | Gateway tools | Notes |
|---------|----------|--------|---------------|-------|
| `orchestrator` | HTTP | ✓ | ✓ | default |
| `strands-agent` | HTTP | ✓ | n/a | gateway/code-interpreter tools not wired in this pattern yet |
| `langgraph-agent` | HTTP | ✓ | ✓ | needs the memory data-plane IAM actions (`ListEvents`) |
| `claude-sdk-agent` | HTTP | ✓ | ✓ | replies include `claude_session_id` |
| `claude-sdk-multi-agent` | HTTP | ✓ | ✓ | delegates to the `code-analyst` subagent |
| `agui-strands-agent` | AGUI | ✓ | ✓ | typed SSE events; verify with `--agui` |
| `agui-langgraph-agent` | AGUI | ✓ | ✓ | slower first response (graph build per request) |

After the matrix run, close the loop on observability — this is what makes module 9's
"end-to-end traces" claim true, and it reuses a span from the invokes above:

```bash
python scripts/check_observability.py --spans
# PASS: trace <id> searchable (5 spans, service agentcore_workshop_dev_orchestrator.DEFAULT)
```

Opt-in (not in `MODULE_VERIFY[9]`) because span delivery lags invocation by a minute or
two. Two facts to keep you from "fixing" working telemetry (verified live, ADOT 0.16.0):
spans are searchable via Logs Insights on `aws/spans` — the path the Transaction Search
console uses — while the classic X-Ray APIs (`batch-get-traces`, `get-trace-summaries`)
only serve the indexed sample (Default rule: 1%), so an empty result there is expected,
not a delivery failure. And `aws-opentelemetry-distro>=0.18` merely moves spans to
per-agent log groups; it is not needed for delivery or search.

If a pattern fails, read the container logs before changing anything — every failure so far
named its own cause there:

```bash
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT" \
  --start-time $(( ($(date +%s) - 900) * 1000 )) \
  --query 'events[].message' --output text | grep -iE 'error|denied|traceback'
```

---

## Known caveats to validate on first live deploy

1. **Interceptor event shape.** The Lambda scans all string leaves in
   `mcp.gatewayRequest` / `mcp.gatewayResponse`. The output contract
   (`interceptorOutputVersion` + `transformedGateway*`) is per AWS docs; confirm the nested
   request/response shape against live Gateway traces for your target type.
2. **Guardrail version.** The interceptor uses the guardrail `DRAFT` version by default. For
   production, publish a numbered version and pin `GUARDRAIL_VERSION`.
3. **Cedar semantics.** The Cedar policies (`control-library/cedar/`) synthesize as text into
   `AWS::BedrockAgentCore::Policy`; CloudFormation does not parse Cedar at synth time. Validate
   allow/deny behaviour on a live gateway in `LOG_ONLY` mode (inspect decision logs) before
   switching `cedar_mode=ENFORCE`. Action names must match `<TargetName>___<tool_name>`.
4. **VPCE service name.** The AgentCore Gateway endpoint uses
   `com.amazonaws.<region>.bedrock-agentcore.gateway` (confirmed). If you also need a Runtime
   data-plane endpoint, confirm its exact service name in your region before adding it.
5. **Traceability depends on CloudTrail.** The EventBridge rule only fires if CloudTrail
   management events are being recorded (the `security` stack / `enable_security` provides a
   trail). Subscribe an endpoint to the SNS topic to actually receive alerts.
6. **Resolved:** the Memory resource policy now uses the native
   `AWS::BedrockAgentCore::ResourcePolicy` L1 (no custom resource / SDK-name guess).
