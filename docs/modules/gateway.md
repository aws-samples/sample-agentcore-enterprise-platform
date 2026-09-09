# Gateway module

The gateway stack (`stacks/gateway_stack.py`) deploys the AgentCore Gateway:
a single MCP endpoint through which every agent discovers and calls tools.
Tools are Lambda targets (your code) or built-in connectors (AWS-operated,
e.g. web search); optional controls add a Bedrock Guardrail egress
interceptor and a Cedar policy engine. Adding a tool here requires no agent
redeploy — see [Adding a gateway target](../GATEWAY_TARGETS.md).

## When it deploys

Per `expected_stacks()` in `infra_utils/platform_config.py`, the
`{project}-{environment}-gateway` stack is in every footprint **except a
federated workload account** — the gateway is a shared service, so workload
accounts consume the platform account's gateway URL via the
`deployment.federation` block instead. It depends on the auth stack (the
JWT issuer).

## What it creates

| Resource | Name | Notes |
|---|---|---|
| `AWS::BedrockAgentCore::Gateway` | `{prefix}-gateway` | `protocol_type=MCP` (versions `2025-03-26`, `2025-06-18`), `authorizer_type=CUSTOM_JWT`, `exception_level=DEBUG` |
| Gateway IAM role | `{prefix}-gateway-role` | Assumed by `bedrock-agentcore.amazonaws.com` |
| Tool Lambda (one per tool) | `{prefix}-tool-<name>` | Python 3.13, ARM64, 30 s timeout, shared execution role |
| `AWS::BedrockAgentCore::GatewayTarget` (per tool) | `<name>` | `GATEWAY_IAM_ROLE` credentials, inline tool schema |
| Web-search connector target (flag) | `web-search` | Built-in connector `web-search` — no Lambda, no API key |
| Egress guardrail (flag) | `{prefix}-egress-guardrail` | `AWS::Bedrock::Guardrail` from `control-library` artifact `guardrail.egress-default` |
| Egress interceptor Lambda (flag) | `{prefix}-egress-interceptor` | Hooked on `REQUEST` + `RESPONSE`, `pass_request_headers=false` |
| Cedar policy engine + policy (flag) | `{project}_{environment}_policy_engine` | One policy: `PermitReadTools` (`cedar.gateway-default.permit-read`) |
| SSM parameter | `/{project}/{environment}/gateway/url` | The MCP endpoint |

Lambda tools are declared in `app.py`'s `tool_configs` dict (the shipped
example is `sample-tool` from `tools/sample_tool/`); the stack creates the
function, the invoke permission, and the target from each entry. The
`gateway.tools` key in `platform.yaml` is schema-validated but the deployed
tool list comes from `tool_configs` — adding a tool means editing `app.py`.

## Configuration

| platform.yaml | Env var / context flag | Default | Effect |
|---|---|---|---|
| `gateway.web_search` | `ENABLE_WEB_SEARCH` / `enable_web_search` | `auto` | `auto` enables the connector only in its launch regions (`us-east-1`, `eu-west-1`, `ap-northeast-1`); creating it elsewhere fails the deploy |
| `security.egress_filter` | `ENABLE_EGRESS_FILTER` / `enable_egress_filter` | `false` | Guardrail + interceptor Lambda on the gateway |
| `security.cedar.enabled` | `ENABLE_CEDAR` / `enable_cedar` | `false` | Attach the Cedar policy engine |
| `security.cedar.mode` | `CEDAR_MODE` / `cedar_mode` | `LOG_ONLY` | `LOG_ONLY` evaluates and logs; `ENFORCE` blocks |

Precedence everywhere: CDK context > env var > `platform.yaml` > default.
Flags match the exact lowercase string `"true"`.

## Interfaces

| Interface | Value |
|---|---|
| SSM | `/{project}/{environment}/gateway/url` — the published [platform interface](../PLATFORM_INTERFACE.md) |
| Stack outputs | `GatewayUrl`, `GatewayArn`, `GatewayId` |
| Consumed by runtimes | Injected as `GATEWAY_URL` (with `GATEWAY_CREDENTIAL_PROVIDER_NAME` from the identity stack) into the orchestrator and research-agent runtimes |

Callers authenticate with a Bearer JWT from the Cognito issuer; allowed
clients are the app client and the M2M client (federated workload runtimes
present the platform M2M client's token).

## Security notes

- **Inbound auth is `CUSTOM_JWT`, always** — there is no unauthenticated
  code path; the authorizer validates against the issuer's OIDC discovery URL.
- **The gateway role is scoped by naming convention**: `lambda:InvokeFunction`
  only on `{prefix}-tool-*`. With web search enabled it also gets
  `bedrock-agentcore:InvokeGateway` (own account) and
  `bedrock-agentcore:InvokeWebSearch` on the service-owned ARN
  `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1` — the literal
  `aws` account id is correct; both actions are documented requirements.
- **Cedar is implicit default-deny**: only explicit read permits ship (a
  blanket `forbid` would override every permit). The shipped permit is
  unconstrained on principal and resource — narrow it before `ENFORCE`. See
  [Security controls](../SECURITY_CONTROLS.md), item 3.
- **The egress interceptor masks, it does not authorize**: it applies the
  guardrail for PII masking and prompt-injection blocking; authorization is
  Cedar's job, behind its own flag.

## Verification

| Tool | What it proves |
|---|---|
| `scripts/verify.py` | Runs `test_gateway.py` whenever `gateway` is in the footprint |
| `scripts/test_gateway.py` | Real MCP `tools/list` + one `tools/call` with a live JWT |
| `scripts/invoke.py --tools` | The tool names agents actually see (`<target>___<tool>`) |

Synth-level checks for the flags (guardrail, interceptor, Cedar engine and
the absence of a `forbid`) are in [`TESTING.md`](../TESTING.md), A6–A7.

## Gotchas

- **Connector and Lambda target configs go through
  `add_property_override`** — the L1 property mapping strips the `lambda`
  key (Python reserved word) and predates connector targets. Set via
  `target_configuration` and the target deploys with no tool, silently.
  `tests/test_web_search_target.py` guards the connector case.
- **The web-search connector is deliberately unpinned** — its default
  version tracks Amazon's improvements; dated pins rot.
- **Cedar names tools as `<TargetName>___<tool_name>`** — the same string
  `invoke.py --tools` prints. A permit naming anything else is dead code
  once `cedar_mode=ENFORCE`.
- **The interceptor uses the guardrail `DRAFT` version** and has no
  try/except around `ApplyGuardrail` — a Bedrock throttle surfaces as a
  Lambda failure, not a defined fail-open/fail-closed decision. Pin a
  published version for production ([`TESTING.md`](../TESTING.md), caveats
  1–2).
- **The default `orchestrator` agent pattern has no tools at all** — to see
  gateway tools used, deploy a tool-consuming pattern
  ([Troubleshooting](../TROUBLESHOOTING.md)).
