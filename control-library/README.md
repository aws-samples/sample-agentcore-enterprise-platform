# control-library

IaC-agnostic **source of truth** for AgentCore security control definitions. Every control is
authored once here, as **valid JSON / Cedar**, and consumed by both:

- **Terraform** — org-scope guardrails (SCPs, later RCPs) under `../terraform/`.
- **CDK (Python)** — account/workload-scope controls, via `infra_utils/policy_loader.py`.

> The name is deliberately **not** "policies" — this folder holds a mix of control types
> (SCP, RCP, resource-based policies, IAM, VPC endpoint policies, Cedar, Guardrails config).
> "policy" alone would be ambiguous with AgentCore Cedar policy and IAM policy.

## Layout

| Folder | Control type | Scope | Primary engine |
|---|---|---|---|
| `scp/` | Service Control Policies | Org | Terraform |
| `rcp/` | Resource Control Policies | Org | Terraform |
| `resource-policies/` | Resource-based policies (Runtime/Memory/Gateway) | Workload | CDK |
| `iam/` | Execution / caller role least-privilege policies | Account | CDK |
| `vpce/` | VPC endpoint policies | Account | CDK |
| `cedar/` | AgentCore Cedar policies (default-forbid on writes) | Workload | CDK |
| `guardrails/` | Bedrock Guardrails configuration | Workload | CDK |

`catalog.yaml` is the machine-readable index. Each entry declares the control id, file, type,
scope, valid attach points, required/optional parameters (with defaults), default enforcement
mode, and which validators must pass in CI.

## Parameterization

Files stay **valid JSON** (so IAM Access Analyzer / `checkov` / `cfn-guard` can lint them).
Per-customer values are marked with sentinel tokens of the form `<<PARAM_NAME>>`.

- **Why `<<...>>` and not `${...}`?** IAM/SCP policy *variables* already use `${aws:...}`
  syntax. Using `${}` for our own params would collide with them and with Terraform
  `templatefile()` interpolation. `<<PARAM>>` is a plain JSON string that neither Python nor
  Terraform `replace()` misinterprets.
- **CDK** injects via `policy_loader.load_control(id, params)`.
- **Terraform** injects via chained `replace()` (or `jsondecode(file(...))` for structured
  overrides).

A control with no required params (e.g. `scp/memory/enforce-cmk.json`) is deployable as-is.

## Adding a control

1. Author the JSON/Cedar file under the right folder.
2. Add an entry to `catalog.yaml` with its param schema and default mode.
3. Ensure it passes the CI validators listed in its `validate:` field.
