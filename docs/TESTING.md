# Testing the AgentCore security-controls updates

How to test everything added on the `feat/security-controls` branch:

- `control-library/` — IaC-agnostic control definitions + `catalog.yaml`
- `infra_utils/policy_loader.py` — loads/parameterizes controls
- **Item 1 (SCP):** CMK-for-Memory SCP applied via `terraform/org-guardrails/`
- **Item 4 (resource policy):** Memory in-account-only resource policy (`enable_resource_policies`)
- **Items 5+6 (Guardrails + interceptor):** egress Lambda interceptor + Bedrock Guardrail
  on the Gateway (`enable_egress_filter`)

There are two layers of testing:

- **Part A — Local (no AWS account needed):** loader, validator, unit tests, `cdk synth`,
  `terraform validate`. This is what CI runs and is enough to review a PR.
- **Part B — Live (needs an AWS account):** deploy and exercise the controls for real.

---

## Prerequisites

```bash
cd "$HOME/Downloads/AC Security Slides/agentcore-accelerator"

# Python venv (repo targets 3.13; 3.12 also works locally)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Node + CDK + Terraform (already present in this environment)
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
# or: python -m pytest tests/ -q      → 10 passed
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

### A5. Memory resource policy synthesizes only when enabled (item 4)

```bash
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1

# Flag OFF → no resource-policy custom resource
rm -rf cdk.out
cdk synth agentcore-workshop-dev-memory 2>/dev/null | grep -c 'Custom::AWS'          # → 0

# Flag ON → PutResourcePolicy custom resource with the rendered policy
rm -rf cdk.out
cdk synth agentcore-workshop-dev-memory \
  -c enable_resource_policies=true -c org_id=o-example123 2>/dev/null \
  | grep -oE 'putResourcePolicy|o-example123|PrincipalOrgID' | sort | uniq -c

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

### A7. Lint (matches CI)

```bash
source .venv/bin/activate
ruff check .
# New security-controls files are ruff-format clean:
ruff format --check infra_utils/policy_loader.py scripts/validate_control_library.py \
  tests/ tools/egress_interceptor/handler.py
```

> Note: `app.py` and `stacks/memory_stack.py` predate ruff-format on `main` and are left in
> their existing style to avoid noisy, unrelated diffs.

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

### B4. Verify the CMK SCP (item 1) — management account

```bash
cd terraform/org-guardrails
terraform apply -var 'target_ids=["ou-abcd-1234wxyz"]'
```

Then, from a member account under that OU, attempt `CreateMemory` **without** a KMS key —
expect an explicit `AccessDenied` from the SCP. Creating with a CMK should succeed.

### B5. Cleanup

```bash
./scripts/deploy.sh destroy --profile security-focused
cd terraform/org-guardrails && terraform destroy && cd ../..
```

---

## Known caveats to validate on first live deploy

1. **Memory resource policy custom resource** uses AWS SDK service `bedrock-agentcore-control`,
   action `putResourcePolicy`. This synthesizes correctly but the exact SDK service/action
   string should be confirmed on first deploy (see the comment in `stacks/memory_stack.py`).
2. **Interceptor event shape.** The Lambda scans all string leaves in
   `mcp.gatewayRequest` / `mcp.gatewayResponse`. The output contract
   (`interceptorOutputVersion` + `transformedGateway*`) is per AWS docs; confirm the nested
   request/response shape against live Gateway traces for your target type.
3. **Guardrail version.** The interceptor uses the guardrail `DRAFT` version by default. For
   production, publish a numbered version and pin `GUARDRAIL_VERSION`.
