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

Every security control below is off by default. Turn them on with feature flags, or use the
`security-focused` profile which enables the account/workload set together (including
`enable_security`, the KMS CMK + CloudTrail stack that item 7 depends on).

Two unrelated flags elsewhere in `app.py` *do* default on — `enable_transaction_search`
(an account- and region-level CloudWatch/X-Ray setting, on because tracing does not work
without it) and `enable_a2a`. On the Terraform side both `enable_scp_memory_enforce_cmk` and
`enable_gateway_scps` default to `true`, so `terraform apply` attaches both SCPs unless you
opt out. Flags are matched against the exact lowercase string `"true"`; `-c enable_cedar=True`
silently does nothing.

## Controls

| # | Control | Flag(s) | Where | Default |
|---|---|---|---|---|
| 1 | **SCP: CMK-for-Memory** | (Terraform vars) | `terraform/org-guardrails/` ← `control-library/scp/` | enforce |
| 1b | **SCP: Gateway configuration hardening** (CMK, no-auth, policy-engine=ENFORCE, approved IdP, protocol, private-endpoint targets, credential-provider, target-type) | `enable_gateway_scps` + vars | `terraform/org-guardrails/gateway.tf` ← `control-library/scp/gateway/` | enforce |
| 2 | **VPC endpoint policy** — action-scoped; org restriction covers **SigV4 callers only** (OAuth/JWT callers carry no IAM principal and pass via `Principal: "*"` per AWS docs). Requires `org_id`: without it the endpoint is created with **no policy at all** | `enable_networking`, `org_id` | `stacks/networking_stack.py` ← `control-library/vpce/` | org-scoped (SigV4) when `org_id` set |
| 2b | **Least-privilege runtime IAM** — SSM reads are path-scoped in `infra_utils/agentcore_role.py`; `control-library/iam/runtime-execution-least-privilege.json` is a **reference policy, not deployed** by any stack (the live role still grants ECR/X-Ray/`PutMetricData` on `Resource: "*"`) | — | `infra_utils/agentcore_role.py` | partial |
| 3 | **AgentCore Cedar policies** | `enable_cedar`, `cedar_mode` | `gateway_stack.py` ← `control-library/cedar/` | LOG_ONLY |
| 4 | **Resource-based policy: Memory in-account-only** | `enable_resource_policies`, `org_id` | `memory_stack.py` ← `control-library/resource-policies/` | enforce |
| 5+6 | **Bedrock Guardrails + egress Lambda interceptor** | `enable_egress_filter` | `gateway_stack.py`, `tools/egress_interceptor/` ← `control-library/guardrails/` | **masking**, not blocking (see below) |
| 7 | **Observability: SNS + EventBridge alerting** | `enable_traceability` | `observability_stack.py` | off |
| 8 | **AgentCore Identity: deny unverified-userId workload tokens**, plus a scoped credential-provider IAM reference policy | `enable_scp_identity_deny_token_for_userid` (Terraform var) | `terraform/org-guardrails/identity.tf` ← `control-library/scp/identity/`, `control-library/iam/` | enforce (denies everyone) |

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
| `enable_security` | `ENABLE_SECURITY` | KMS CMK + CloudTrail; **prerequisite for item 7** (needs management events) |
| `org_id` | `ORG_ID` | Org ID (o-xxxx) required by items 2 and 4 |

Item 4 hard-fails without `org_id`; item 2 silently ships an unpolicied endpoint instead.
`scripts/deploy.sh` fails fast (or prompts) when resource policies are on and `ORG_ID` is empty.

## Quick start

```bash
# Everything account/workload-scope, in safe (log-only / masking) defaults:
export ORG_ID=o-yourorgid
./scripts/deploy.sh deploy --profile security-focused

# Or one control at a time (NON_INTERACTIVE is read by deploy.sh only, not by cdk):
cdk deploy agentcore-workshop-dev-gateway -c enable_cedar=true

# Org guardrails (from the Organizations management account):
cd terraform/org-guardrails && terraform init && terraform apply -var 'target_ids=["ou-..."]'
```

## Egress interceptor design (items 5+6)

- **Where it hooks:** on the Gateway target path as an interceptor Lambda
  (`tools/egress_interceptor/`), inspecting requests and responses before egress to
  tools/targets.
- **What it does:** PII masking/filtering and prompt-injection checks via Bedrock
  Guardrails (`ApplyGuardrail`), config sourced from `control-library/guardrails/`. It performs
  **no authorization** — Cedar (item 3) is a separate control behind a separate flag, and
  `enable_egress_filter` alone gives you none of it.
- **Masking, not blocking:** the handler raises only when an assessment comes back
  `action == "BLOCKED"`; otherwise it substitutes anonymized text and the request proceeds.
- **Caveats:** the `gatewayRequest`/`gatewayResponse` shape is unvalidated, so the handler
  scans every string leaf generically and passes payload shapes it does not recognise through
  **unchanged**. There is no try/except around `ApplyGuardrail`, so a Bedrock throttle surfaces
  as a Lambda failure rather than a defined fail-open/fail-closed decision. Validate against
  live Gateway traces before relying on it in production.
- **Toggle:** `enable_egress_filter`, deployed by `gateway_stack.py` like every other flag.

## Cedar authorization (item 3)

Cedar is implicit-deny: anything not explicitly permitted is denied. Note the repo previously
shipped a blanket `forbid` alongside the read permit, which was removed — in Cedar a matching
`forbid` overrides every `permit`, so it made the permit dead code and denied everything.

What that means in practice, before you describe this as a default-deny gateway:

1. `enable_cedar` defaults off, so no policy engine is attached and no Cedar evaluation happens.
2. `cedar_mode` defaults to `LOG_ONLY`, so decisions are logged, not enforced. Nothing is
   actually denied until `cedar_mode=ENFORCE`.
3. The one shipped policy is unconstrained on principal and resource
   (`permit(principal, action in [AgentCore::Action::"<<read_action>>"], resource);`), so any
   authenticated caller may invoke the sample tool on any gateway. Narrow it before enforcing.

## Adding a control

1. Add the JSON/Cedar file under `control-library/<type>/` and register it in `catalog.yaml`.
2. Load it in a stack via `policy_loader.load_control[_json|_text]` behind a feature flag,
   or reference it from a Terraform module with `replace()`.
3. `make validate-controls && make test-controls` — then add a synth check to `TESTING.md`.

## AgentCore Identity controls (item 8)

Two controls covering the token path into the credential vault.

**SCP: deny `GetWorkloadAccessTokenForUserId`.** That API takes the user identifier as an
unverified string. Any principal holding the action can mint a workload access token for any
user and read that user's stored credentials out of the token vault — no JWT, no proof of
identity anywhere in the call. Agents behind Runtime or Gateway inbound auth never need it:
the caller's verified token arrives with the request, and `GetWorkloadAccessTokenForJWT` is
the path that checks it.

The exemption parameter defaults to a role ARN that cannot exist, so the control denies
everyone until an operator supplies a real pattern. Narrow it only for a genuine break-glass
or migration path, and prefer removing the need over widening the pattern:

```bash
cd terraform/org-guardrails && terraform apply \
  -var 'target_ids=["ou-example-11111111"]' \
  -var 'identity_approved_principal_arn_pattern=arn:aws:iam::111122223333:role/break-glass'
```

If you need the userId path for a specific service and want something narrower than an ARN
exemption, the action also supports the `bedrock-agentcore:userid` condition key, so the deny
can be scoped by user identifier instead. The blanket form is the safer default.

Quota note: this is a standalone SCP, bringing the module to 3 attachments per target against
4 usable slots (the 5-per-target limit minus `FullAWSAccess`). A fourth standalone SCP is the
last one that fits — beyond that, merge statements the way `gateway.tf` does.

**IAM: `iam.identity-credential-provider-scoped`.** A reference policy for an agent's own
execution role. AgentCore [does not enforce any binding][scope-cp] between a workload identity
and the credential providers it may read, so IAM is the only fence: one workload identity and
one role per trust boundary, each naming exactly one provider. A shared execution role hands
every agent every provider's credentials.

Note each `Allow` lists the parent resources as well as the leaf. `GetResourceOauth2Token` and
the `GetWorkloadAccessToken*` actions declare several **required** resource types — the
directory and the token vault as well as the workload identity and the provider — so a
statement naming only the provider ARN authorises nothing. The resulting `AccessDenied` is
easy to "fix" by widening `Resource` to `"*"`, which defeats the control entirely. `Deny`
statements need only the specific ARN they target.

Like `iam.runtime-execution-least-privilege`, this is a template —
`infra_utils/agentcore_role.py` does not read AgentCore Identity today, so nothing deploys it
automatically.

Retrieving a token is also not the same as retrieving credentials: what a workload gets back
is scoped to the user identity in its workload access token, and for OAuth2 3LO providers the
end user must have completed authorization before any credentials exist to return.

[scope-cp]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/scope-credential-provider-access.html

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

**They deploy as one policy, not eight.** Organizations allows only 5 SCPs per target (4 usable
after `FullAWSAccess`), so `gateway.tf` flattens all eight documents into a single consolidated
SCP, `${name_prefix}-scp-gateway-guardrails`. Consequences: `enable_gateway_scps` is
all-or-nothing (subsetting means editing the map in `gateway.tf`), and a `lifecycle`
precondition enforces the 5,120-character SCP limit plus Sid uniqueness — a ninth control can
fail at plan time. The `for_each` is on the *attachment*, over `var.target_ids`.

**"Fully-private Gateway" bundle:** `deny-no-auth` + `require-policy-engine` +
`targets-require-private-endpoint`. These are control-plane controls: they constrain how a
gateway may be configured, not who may invoke one.

For invoke-time restrictions, per-gateway resource-based policies attached with the native
`AWS::BedrockAgentCore::ResourcePolicy` (as used for Memory in item 4) are the mechanism this
repo uses today.

## Safe rollout

- Cedar ships `LOG_ONLY`; validate decision logs before `cedar_mode=ENFORCE`, and narrow the
  shipped permit's principal/resource first.
- SCPs are additive-deny; attach to a sandbox OU first.
- Guardrail resolves to the `DRAFT` version (via `guardrail.attr_version`, not a hardcoded
  string); pin a published version for production.
- Subscribe an endpoint to the item-7 SNS topic to actually receive alerts. The topic is not
  KMS-encrypted — add a CMK if alert contents are sensitive in your environment.
- Item 7 needs CloudTrail management events, so enable it together with `enable_security`.
