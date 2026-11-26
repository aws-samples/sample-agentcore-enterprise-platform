# v0.1.0 Support Matrix

This matrix separates implemented code from live evidence. “Live verified”
means the path was exercised in a development account and the dated evidence
is retained in this repository's work log or linked documentation. It is not a
customer production approval.

## Deployment and topology

| Path | v0.1.0 status | Evidence and boundary |
|---|---|---|
| Centralized workshop, `us-east-1` | **Live verified** | Greenfield platform verified on 2026-09-20; migration rehearsal verified on 2026-09-21 |
| Federated platform + workload accounts, `us-east-1` | **Live verified** | Two-account OAuth path, Gateway calls, and role-specific stacks verified on 2026-08-16; see [Multi-account strategies](MULTI_ACCOUNT.md?id=verified) |
| Distributed copies | **Implemented; automated evidence** | Configuration and stack contracts synthesize; each account is an independent centralized copy. No v0.1.0 multi-account live rehearsal |
| Production mode | **Implemented; not launch-approved** | Fail-closed controls and retained lifecycle synthesize and have automated tests. No production deployment, restore rehearsal, or operational readiness approval |
| Regions other than `us-east-1` | **Customer validation required** | Service and model availability vary. Web search is enabled automatically only in its declared launch Regions. Run `doctor`, build, and full verification in the selected Region |

### Region tables used by `design`

`design` runs static Region checks (`region_findings` in
`infra_utils/platform_config.py`). These checks make no AWS calls, and every
message names the Region:

- **Zones (warning):** networking is on and the Region has no
  `SUPPORTED_ZONE_IDS` entry in `infra_utils/runtime_network.py`. A missing
  entry means the table has not been updated for that Region. It does not
  mean the Region is unsupported, and the runtime networking helper applies
  no zone restriction there.
- **Models (warning):** `agents.model_id` or an `agents.allowed_models`
  entry is either not in `INFERENCE_PROFILES_BY_GEO` or uses a profile
  prefix other than the Region's geo (`REGION_GEO`). `global.` profiles are
  accepted everywhere.
- **Web search (warning):** `gateway.web_search: on` is set outside
  `WEB_SEARCH_REGIONS`.

The zone, model and web search tables drift from AWS offerings, which is why
findings from them are warnings. `doctor` and `verify` remain the
authoritative live checks. To update the tables, edit them in
`infra_utils/platform_config.py`. When AWS adds a profile, add its base model
id (without the geo prefix) to each geo set that offers it. Add new Regions to
`REGION_GEO`. Run `python -m pytest tests/test_region_validation.py -q`
afterwards.

## Identity

| Path | v0.1.0 status | Evidence and boundary |
|---|---|---|
| Cognito issuer | **Live verified** | Used by the isolated migration rehearsal and platform verification |
| Entra ID brokered through Cognito | **Live verified** | End-to-end authorization-code, token exchange, and agent invocation evidence is recorded in [Enterprise IdP](ENTERPRISE_IDP.md?id=verified) |
| Entra ID as direct issuer | **Implemented; automated evidence** | Schema, infrastructure, and verification paths exist. Run a customer-tenant authorization-code and M2M test before use |
| Okta or Ping brokered through Cognito | **Implemented; customer validation required** | Provider construction is implemented; no v0.1.0 live tenant evidence |
| Okta as direct issuer | **Implemented; automated evidence, live evidence pending** | Schema, infrastructure, and verification paths exist (custom authorization server, `identity.audience`, `identity.m2m_scope`). No v0.1.0 live tenant evidence; run a customer-tenant authorization-code and M2M test before use |
| Ping as direct issuer | **Not supported** | Ping deploys brokered through Cognito |

## Agent and model paths

| Path | v0.1.0 status | Evidence and boundary |
|---|---|---|
| `orchestrator`, `strands-agent`, `langgraph-agent`, `claude-sdk-agent`, `claude-sdk-multi-agent`, `agui-strands-agent`, `agui-langgraph-agent` | **Prior live evidence; revalidate per release/account** | The documented live matrix passed for all seven patterns. It was not re-run from the v0.1.0 tag; follow [Testing](TESTING.md?id=part-c-agent-pattern-matrix-live) |
| Cross-Region inference profiles | **Implemented** | `doctor` checks profile metadata; `verify` performs actual invocation after deployment |
| Model entitlement in a new account | **Customer validation required** | Metadata discovery is not an invocation. Account/Region access and Marketplace prerequisites can still block runtime calls |
| AI quality and task-specific evaluation | **Not included** | The accelerator verifies platform behavior, not whether a customer's agent meets a quality threshold |

## Migration

| Path | v0.1.0 status | Evidence and boundary |
|---|---|---|
| Source built by CodeBuild as arm64 + supplied adapter + AgentCore Runtime | **Live verified** | Isolated fixture build, invocation, verification, rollback, destroy, and residue audit passed on 2026-09-21 |
| Pre-built arm64 image + supplied adapter | **Implemented; architecture must be proven** | Configuration and synthesis are covered. `doctor` cannot inspect every private registry; customer must prove `linux/arm64` and pin a digest |
| Retain existing private datastore | **Evidence-gated** | `retain-source-v1` moves no data. Customer connectivity, classification, identity mapping, validation, and approval remain required |
| Data copy, trigger shadowing, and traffic cutover | **Customer-operated plans only** | Readiness validates digests, evidence, rollback, and approvals. The accelerator deliberately has no generic execute command |
| amd64-only image, ECS-on-EC2 target, or native migration mode | **Not supported** | The supported target is an arm64 AgentCore Runtime in adapter mode |

## Security controls

Optional controls have schema, synthesis, and focused tests. Their effectiveness
still depends on the customer's identities, tools, policies, data, Region, and
organization guardrails. Cedar must be exercised in `LOG_ONLY` before
`ENFORCE`; the Bedrock Guardrail must be published and pinned for a production
design. See [Security controls](SECURITY_CONTROLS.md) and
[known limitations](KNOWN_LIMITATIONS.md).
