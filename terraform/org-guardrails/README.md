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
| `scp.gateway.*` (8 controls, **1 consolidated SCP**, `gateway.tf`) | `aws_organizations_policy` + attachment | Gateway configuration hardening: CMK, no-auth, policy-engine=ENFORCE, approved IdP, protocol, private-endpoint targets, credential-provider, target-type. Toggle with `enable_gateway_scps`. |

### Why the gateway controls are consolidated into one SCP

AWS Organizations allows at most **5 SCPs attached** to any single root/OU/account, and the
default `FullAWSAccess` policy already occupies one slot — leaving **4 usable**. Attaching
the 8 gateway controls as individual SCPs plus the memory SCP would require **9 slots** and
fail against any real target.

So this module merges the `Statement` arrays of all 8 gateway policy files into **one**
consolidated SCP at apply time (after sentinel injection). With everything enabled the module
attaches **2 SCPs per target** (1 consolidated gateway + 1 memory) — well under the quota.

The per-control JSON files in `control-library/scp/gateway/` remain the granular,
per-control source of truth: SCPs are additive-deny, so merging statements into one document
is semantically identical to attaching them separately. Plan-time preconditions guard the
merge: the consolidated document must stay under the **5,120-character** SCP size quota, and
every statement `Sid` must be unique across the library files.

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
terraform plan    # with defaults: 2 policies + 2 attachments per target
terraform apply
```

SCPs are **additive-deny** and layer on top of the existing `FullAWSAccess` policy, so
attaching this module is non-destructive to existing permissions. With all controls enabled
the module consumes **2** of the 4 usable SCP slots per target (see the consolidation note
above), leaving room for your organization's other policies.

`target_ids` entries are validated at plan time (`r-...` root, `ou-...-...` OU, or 12-digit
account ID) so a typo fails fast instead of surfacing as an opaque Organizations API error.

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
already runs. Customers consuming the files directly can pick individual controls (each file
is a complete, standalone SCP) — just mind the 5-attachments-per-target quota and merge
`Statement` arrays as this module does if they need more than 4 controls on one target.

## Adding more SCPs

1. Add the raw policy to `control-library/scp/<service>/<name>.json` and register it in
   `control-library/catalog.yaml`. Give every statement a `Sid` that is unique across the
   library (a plan-time precondition enforces this for the gateway merge).
2. For a **gateway** control: add an entry to `local.gateway_scp_rendered` in `gateway.tf`
   (a `replace()` per sentinel) — its statements are merged into the consolidated gateway SCP
   automatically. The plan-time size precondition will fail loudly if the merged document
   exceeds the 5,120-character SCP quota.
3. For a control on a **new service surface**: add an `enable_scp_<name>` variable, a
   `replace()` local, and `aws_organizations_policy` + attachment resources — but keep the
   total attachments per target at 4 or fewer (5-per-target quota minus `FullAWSAccess`).
   Prefer merging into an existing consolidated policy when the surface fits.
