# terraform/org-guardrails

Reference Terraform module for applying **org-scope** AgentCore guardrails (Service Control
Policies today; Resource Control Policies later).

This is the Terraform half of the **scope-split** model:

- **Terraform (this module)** owns org-scope guardrails (SCPs/RCPs).
- **CDK (Python)** owns account/workload-scope controls (resource policies, IAM, VPCE, Cedar,
  Guardrails, interceptor).

Both read the **same** raw policy files from [`control-library/`](../../control-library) — one
source of truth, no duplicated policy bodies.

## What it deploys

| Control id | Resource | Default |
|---|---|---|
| `scp.memory.enforce-cmk` | `aws_organizations_policy` + attachment | Deny `CreateMemory` unless a CMK is specified |

## Prerequisites

- Run from the **Organizations management account** or a **delegated administrator** for SCPs.
- **SCPs must be enabled** for the organization (`aws organizations enable-policy-type`).
- Credentials with `organizations:CreatePolicy` / `AttachPolicy` permissions.

## Usage

```hcl
module "agentcore_org_guardrails" {
  source = "../../terraform/org-guardrails"

  name_prefix = "agentcore"
  target_ids  = ["ou-abcd-1234wxyz"] # OU(s) or account(s) to attach to

  # Optional: narrow the required CMK pattern (default requires any CMK)
  # kms_key_arn_pattern = "arn:aws:kms:*:111122223333:key/*"
}
```

```bash
terraform init
terraform plan
terraform apply
```

SCPs are **additive-deny** and layer on top of the existing `FullAWSAccess` policy, so
attaching this module is non-destructive to existing permissions.

## Parameterization

The raw SCP in `control-library/scp/memory/enforce-cmk.json` uses the `<<kms_key_arn_pattern>>`
sentinel. This module injects the value with Terraform's `replace()` (not `templatefile()`),
so any IAM/SCP policy variables (`${aws:...}`) inside a policy body are left untouched.

## Supporting other customer deployment models

The raw policy is decoupled from the apply mechanism, so customers who don't use standalone
Terraform can consume the **same** `control-library/scp/*.json` files via:

- **AWS Control Tower** — proactive/preventive controls or custom SCPs.
- **Account Factory for Terraform (AFT)** — reference this module from AFT customizations.
- **CloudFormation StackSets** — `AWS::Organizations::Policy` with the same JSON body.
- **Manual / console** — paste the rendered JSON.

Terraform is the path we ship and test; the JSON is portable to whichever model the customer
already runs.

## Adding more SCPs

1. Add the raw policy to `control-library/scp/<service>/<name>.json` and register it in
   `control-library/catalog.yaml`.
2. Add an `enable_scp_<name>` variable, a `replace()` local (one per sentinel), and the
   `aws_organizations_policy` + attachment resources here.
