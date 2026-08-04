# AgentCore Security Controls

Reusable, opt-in security building blocks for the AgentCore accelerator. Every control is
defined once in an IaC-agnostic **control-library** and consumed by CDK (account/workload
scope) or Terraform (org scope). How to test everything: [`TESTING.md`](TESTING.md).

**Why one library, and why valid JSON/Cedar:** authoring each control once means a policy
fix lands in both engines at the same time — no duplicated policy bodies drifting across
languages. Keeping artifacts as valid JSON (rather than templated `.tftpl`) means policy
linters (checkov, IAM Access Analyzer, cfn-guard) can scan every file in CI — a real trust
signal for a security accelerator. Parameters use `<<sentinel>>` tokens injected at deploy
time, never template syntax that would break linting.

## The model (scope-split)

- **`control-library/`** — single source of truth. Valid JSON / Cedar with `<<sentinel>>`
  parameters, indexed by `catalog.yaml`. See [`../control-library/README.md`](../control-library/README.md).
- **Terraform** (`terraform/org-guardrails/`) — org-scope guardrails (SCPs).
- **CDK (Python)** — account/workload-scope controls, loaded via
  `infra_utils/policy_loader.py` and toggled by feature flags.

Nothing is on by default. Turn controls on with feature flags, or use the
`security-focused` profile which enables the account/workload set together.

## Controls

| # | Control | Flag(s) | Where | Default |
|---|---|---|---|---|
| 1 | **SCP: CMK-for-Memory** | (Terraform vars) | `terraform/org-guardrails/` ← `control-library/scp/` | enforce |
| 1b | **SCP: Gateway configuration hardening** (CMK, no-auth, policy-engine=ENFORCE, approved IdP, protocol, private-endpoint targets, credential-provider, target-type) | `enable_gateway_scps` + vars | `terraform/org-guardrails/gateway.tf` ← `control-library/scp/gateway/` | enforce |
| 2 | **VPC endpoint policy + least-privilege IAM** | `enable_networking`, `org_id` | `networking_stack.py`, `agentcore_role.py` ← `control-library/vpce/`, `iam/` | org-scoped |
| 3 | **AgentCore Cedar policies** | `enable_cedar`, `cedar_mode` | `gateway_stack.py` ← `control-library/cedar/` | LOG_ONLY |
| 4 | **Resource-based policy: Memory in-account-only** | `enable_resource_policies`, `org_id` | `memory_stack.py` ← `control-library/resource-policies/` | enforce |
| 5+6 | **Bedrock Guardrails + egress Lambda interceptor** | `enable_egress_filter` | `gateway_stack.py`, `tools/egress_interceptor/` ← `control-library/guardrails/` | enforce (guardrail) |
| 7 | **Observability: SNS + EventBridge alerting** | `enable_traceability` | `observability_stack.py` | off |

## Feature flags

Set via CDK context (`-c flag=value`) or environment variable (`FLAG=value`):

| Flag | Env | Purpose |
|---|---|---|
| `enable_resource_policies` | `ENABLE_RESOURCE_POLICIES` | Memory resource-based policy (item 4) |
| `enable_egress_filter` | `ENABLE_EGRESS_FILTER` | Guardrail + interceptor (items 5+6) |
| `enable_cedar` | `ENABLE_CEDAR` | Cedar policy engine (item 3) |
| `cedar_mode` | `CEDAR_MODE` | `LOG_ONLY` (default) or `ENFORCE` |
| `enable_traceability` | `ENABLE_TRACEABILITY` | Alerting on sensitive API calls (item 7) |
| `enable_networking` | `ENABLE_NETWORKING` | VPC + AgentCore endpoint policy (item 2) |
| `org_id` | `ORG_ID` | Org ID (o-xxxx) required by items 2 and 4 |

## Quick start

```bash
# Everything account/workload-scope, in safe (log-only / masking) defaults:
export ORG_ID=o-yourorgid
./scripts/deploy.sh deploy --profile security-focused

# Or one control at a time:
NON_INTERACTIVE=1 cdk deploy agentcore-workshop-dev-gateway -c enable_cedar=true

# Org guardrails (from the Organizations management account):
cd terraform/org-guardrails && terraform init && terraform apply -var 'target_ids=["ou-..."]'
```

## Egress interceptor design (items 5+6)

- **Where it hooks:** on the Gateway target path as an interceptor Lambda
  (`tools/egress_interceptor/`), inspecting requests and responses before egress to
  tools/targets.
- **What it does:** PII masking/filtering and prompt-injection checks via Bedrock
  Guardrails (`ApplyGuardrail`), config sourced from `control-library/guardrails/`;
  authorization via the gateway's Cedar policy engine (default-forbid posture) from
  `control-library/cedar/`.
- **Toggle:** `enable_egress_filter`, deployed by `gateway_stack.py` like every other flag.

## Adding a control

1. Add the JSON/Cedar file under `control-library/<type>/` and register it in `catalog.yaml`.
2. Load it in a stack via `policy_loader.load_control[_json|_text]` behind a feature flag,
   or reference it from a Terraform module with `replace()`.
3. `make validate-controls && make test-controls` — then add a synth check to `TESTING.md`.

## Gateway configuration hardening (control-plane SCPs)

Enterprise guardrails built on the launched AgentCore **Gateway configuration** condition keys.
They constrain what admins can create/update (control plane), enforced via
`terraform/org-guardrails/`:

| SCP | Condition key | Effect |
|---|---|---|
| `require-cmk` | `KmsKeyArn` | Gateway must use a (approved) CMK |
| `deny-no-auth` | `GatewayAuthorizerType` | Block `NONE` (no unauthenticated gateways) |
| `require-policy-engine` | `PolicyEngineArn` / `PolicyEngineMode` | Require a Cedar policy engine in `ENFORCE` |
| `enforce-approved-idp` | `DiscoveryUrl` | JWT gateways must use an approved IdP |
| `restrict-protocol` | `ProtocolType` | Restrict explicitly-set protocols to `MCP` (an omitted protocol falls back to the service default, unconstrained) |
| `targets-require-private-endpoint` | `PrivateEndpointType` | Targets must use a private endpoint (private egress) |
| `targets-restrict-credential-provider` | `CredentialProviderType` | Deny `API_KEY` / `JWT_PASSTHROUGH` |
| `targets-restrict-type` | `McpTargetConfigurationType` | Allow-list target types (lambda, mcpServer) |

**"Fully-private Gateway" bundle:** `deny-no-auth` + `require-policy-engine` +
`targets-require-private-endpoint` (control plane). The data-plane half — locking down *who
can invoke* — is on the roadmap below.

### Roadmap (not yet launched)

Data-plane condition keys and RCP support are launching later. When available, this repo will
add (as `control-library/rcp/` + Terraform `RESOURCE_CONTROL_POLICY`):

- **Private ingress:** `aws:SourceVpc` / `aws:SourceVpce` / `aws:VpceOrgID` on `InvokeGateway`
  — the universal ingress lockdown that also covers OAuth callers (SCPs only reach SigV4
  callers). Requires enabling the RCP policy type + org onboarding.
- **Inbound JWT claims:** `InboundJwtClaim/{aud,client_id,iss,scope,sub}` to restrict OAuth
  callers by claim, via RCP or per-gateway resource-based policy.

Until then, per-gateway resource-based policies (attached with the native
`AWS::BedrockAgentCore::ResourcePolicy`, as used for Memory) are the available data-plane
fallback.

## Safe rollout

- Cedar ships `LOG_ONLY`; validate decision logs before `cedar_mode=ENFORCE`.
- SCPs are additive-deny; attach to a sandbox OU first.
- Guardrail uses the `DRAFT` version; pin a published version for production.
- Subscribe an endpoint to the item-7 SNS topic to actually receive alerts.
