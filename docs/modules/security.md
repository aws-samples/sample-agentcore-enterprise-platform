<!-- markdownlint-disable MD041 -->

# Security module

The `security` stack (`stacks/security_stack.py`) is small on purpose: a KMS
customer-managed key and a CloudTrail audit trail. Most of the platform's
security surface deliberately lives *inside the stacks it protects* — an IAM
deny in the runtime stack, a policy engine on the gateway — and is toggled by
its own flag. This page maps that surface. The control-by-control reference,
including the org-scope SCPs and their caveats, is
[Security controls](../SECURITY_CONTROLS.md) — read that before enforcing
anything; this page does not duplicate it.

## When it deploys

| Path | Setting |
|---|---|
| `platform.yaml` | `security.cloudtrail_alerting: true` |
| Env var / CDK context | `ENABLE_SECURITY=true` / `-c enable_security=true` |
| Guided workshop | Module E (Security Automation) |
| Preset | `security-focused` (with the rest of the account-scope set) and `platform-team` enable it |

Off by default. In a federated deployment it is not role-gated in `app.py` —
it deploys wherever the flag is on.

## What it creates

| Resource | Details |
|---|---|
| KMS CMK | Alias `alias/<project>-<env>-agentcore`, key rotation enabled, `RemovalPolicy.DESTROY` |
| S3 bucket | `<project>-<env>-cloudtrail-<account>`, S3-managed encryption, auto-delete on destroy |
| CloudTrail trail | `<project>-<env>-agentcore-trail`, single-region, logging to that bucket |

## Configuration

| Key (platform.yaml) | Env var | Default | Effect |
|---|---|---|---|
| `security.cloudtrail_alerting` | `ENABLE_SECURITY` | `false` | Creates this stack |

The stack's own `enable_kms` / `enable_cloudtrail` constructor parameters are
both hardwired to `true` by `app.py` — there is no flag to deploy one without
the other.

## Interfaces

- `KmsKeyArn` CloudFormation output.
- The `memory` stack consumes `kms_key.key_arn` directly when both stacks are
  enabled, encrypting AgentCore Memory with the CMK (and takes a stack
  dependency on `security`). Without this stack, Memory uses service-managed
  encryption.
- The `enable_traceability` alerting in the `observability` stack only fires
  on CloudTrail management events, so it *requires* this stack — the
  `platform.yaml` validator refuses `security.traceability: true` without
  `security.cloudtrail_alerting: true`.

## The security surface lives mostly elsewhere

These controls are part of the platform's security posture but deploy inside
other stacks, each behind its own flag:

| Control | Flag | Lives in | Page |
|---|---|---|---|
| Memory resource policy (in-account-only, org deny guard) | `enable_resource_policies` + `org_id` | `memory` stack | [memory.md](memory.md) |
| Egress filter (Bedrock Guardrail + interceptor Lambda, PII masking) | `enable_egress_filter` | `gateway` stack | [gateway.md](gateway.md) |
| Cedar policy engine (`LOG_ONLY`/`ENFORCE`) | `enable_cedar`, `cedar_mode` | `gateway` stack | [gateway.md](gateway.md) |
| Guardrailed-only inference (IAM `Null`-deny + baseline guardrail per runtime) | `require_guardrails` | `runtime-*` stacks | [runtime.md](runtime.md) |
| Model allow-list (runtime role Bedrock IAM scoped to `agents.allowed_models`) | `allowed_models` | `runtime-*` stacks | [runtime.md](runtime.md) |
| Traceability (SNS + EventBridge alerting on sensitive API calls) | `enable_traceability` | `observability` stack | [observability.md](observability.md) |
| VPC endpoint policy (org-scoped, SigV4 callers only) | `enable_networking` + `org_id` | `networking` stack | [networking.md](networking.md) |
| Org-level SCPs (Memory CMK, gateway hardening, identity token path) | Terraform variables | `terraform/org-guardrails/` | [Security controls](../SECURITY_CONTROLS.md) |

All of them source their policy bodies from `control-library/` (indexed by
`catalog.yaml`), authored once as valid JSON/Cedar and consumed by CDK
(account/workload scope) or Terraform (org scope).

## Security notes

- Removal policies are `DESTROY` and the trail bucket auto-deletes: destroying
  the stack deletes the audit history. That is the workshop posture — retain
  the bucket for production.
- The trail is single-region and records management events only.
- The trail bucket uses S3-managed encryption, not the stack's own CMK.
- The CMK protects Memory; nothing else in the platform consumes it today.

## Verification

There is no dedicated live check for this stack itself; `scripts/verify.py`
composes checks from the deployment contract, and the controls this page maps
carry their own:

- `scripts/check_guardrail_enforcement.py` — IAM simulation that inference
  without a guardrail is denied (runs when `require_guardrails` is on).
- `scripts/check_network.py` — endpoint/VPC placement (networking).
- Synth-time checks for each control: [`TESTING.md`](../TESTING.md) part A.

## Gotchas

- **Traceability needs this stack.** `security.traceability` without
  `security.cloudtrail_alerting` is a validation error, because the alert rule
  would silently never fire.
- **The org SCPs can lock the platform out.** `enable_scp_memory_enforce_cmk`
  and `enable_gateway_scps` deny operations the platform's own *default*
  configuration performs; attach them only after every covered account runs
  the prerequisites (see the compatibility preflight in
  [Security controls](../SECURITY_CONTROLS.md)).
- **Flags match the exact string `"true"`.** `-c enable_security=True`
  silently does nothing.
- **Cedar and the egress filter are separate controls.** Enabling the egress
  filter gives you masking, not authorization; Cedar ships `LOG_ONLY` and
  enforces nothing until `cedar_mode=ENFORCE`.
