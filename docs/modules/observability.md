# Observability module

The observability stack (`stacks/observability_stack.py`) makes the platform
watchable: it enables CloudWatch Transaction Search (without which X-Ray
rejects every span batch), wires vended log delivery for each AgentCore
resource, and optionally adds CloudTrail-backed alerting on sensitive API
calls plus a CloudWatch alarms + dashboard layer. Trace mechanics and span
visibility are covered in [Tracing](../TRACING.md).

## When it deploys

Per `expected_stacks()` in `infra_utils/platform_config.py`, the
`{prefix}-observability` stack is in **every** footprint, on both sides of a
federation — observability is per-account, each side monitors what it runs.
`app.py` passes it a `monitored_resources` map of whatever gateway, memory,
and runtime stacks actually exist in this account, and it depends on all of
them.

## What it creates

| Resource | Condition | Notes |
|---|---|---|
| CloudWatch Logs resource policy `{prefix}-transaction-search-xray` | `transaction_search` (default on) | Lets X-Ray write the span log groups (`aws/spans`, `/aws/application-signals/data`); confused-deputy guarded by `SourceArn`/`SourceAccount` |
| Custom resource: `xray:UpdateTraceSegmentDestination` → `CloudWatchLogs` | `transaction_search` | `AwsCustomResource` with **no onDelete** — see Gotchas |
| Log group per monitored resource | always | `/aws/bedrock-agentcore/{prefix}/{resource}`, 1-month retention |
| `AWS::Logs::DeliverySource` / `DeliveryDestination` / `Delivery` per resource | always | Vended `APPLICATION_LOGS` delivery into the log group above |
| SNS topic `{prefix}-platform-alarms` | `alarms` | Alarm + OK actions for every alarm; optional email subscription |
| CloudWatch alarms (per resource, below) | `alarms` | All treat missing data as `NOT_BREACHING` |
| CloudWatch dashboard `{prefix}-platform` | `alarms` | Traffic, errors, latency per resource + account-level Bedrock widgets |
| SNS topic `{prefix}-agentcore-security-alerts` + EventBridge rule | `traceability` | Fires on sensitive AgentCore config-change API calls (`CreateGateway`, `DeleteMemory`, `PutResourcePolicy`, …) via CloudTrail |

## Metric namespaces and dimensions

Every metric name and dimension set was **read off a live deployment** — the
`AWS/Bedrock-AgentCore` namespace is not documented well enough to guess, and
a wrong dimension is an alarm that can never fire. `tests/test_alarms.py`
pins the load-bearing strings. The namespace is hyphenated:
`AWS/Bedrock-AgentCore`, not `AWS/BedrockAgentCore` (which is empty).

| Resource | Dimensions | Alarmed metrics |
|---|---|---|
| Runtime | `Resource` (ARN), `Operation=InvokeAgentRuntime`, `Name={project}_{environment}_{component}::DEFAULT` | `SystemErrors` (2 periods), `Throttles`, `Latency` p99 > 30 s for 15 min |
| Gateway | `Resource`, `Operation=InvokeGateway`, `Protocol=MCP` | `SystemErrors`, `Throttles`, `Latency` p99 > 5 s |
| Memory | `Resource`, `Operation` (per-operation; alarms cover `CreateEvent` and `ListEvents` — the two every agent turn depends on) | `SystemErrors`, `Throttles` |
| Bedrock (account) | `AWS/Bedrock`, aggregate series carry **no dimensions**; `EstimatedTPMQuotaUsage` is per-`ModelId` only (dashboard uses `SEARCH`) | `InvocationThrottles` >= 5 per 5 min, `InvocationServerErrors` (2 periods) |
| Sessions (account) | `ActiveSessionCount` with `Service=AgentCore.Runtime` | dashboard only |

The runtime `Name` dimension mirrors `RuntimeStack`'s name derivation
(underscores); `tests/test_alarms.py` fails if the two drift.

## Configuration

| platform.yaml | Env var / context flag | Default | Effect |
|---|---|---|---|
| `observability.transaction_search` | `ENABLE_TRANSACTION_SEARCH` / `enable_transaction_search` | `true` | Account/region trace destination + span resource policy. Turn **off** where a platform team owns tracing centrally |
| `observability.alarms` | `ENABLE_ALARMS` / `enable_alarms` | `false` | SNS ops topic + alarms + dashboard |
| `observability.alarm_email` | `ALARM_EMAIL` / `alarm_email` | empty | Email subscription on the ops topic; empty means the topic deploys unsubscribed. Placeholder addresses are rejected at validation |
| `security.traceability` | `ENABLE_TRACEABILITY` / `enable_traceability` | `false` | Security-alerts topic + EventBridge rule. The validator requires `security.cloudtrail_alerting` with it — without a trail the rule silently never fires |

## Interfaces

| Interface | Value |
|---|---|
| Log groups | `/aws/bedrock-agentcore/{prefix}/<resource>` per monitored resource; spans land in the shared `aws/spans` group |
| Stack outputs | `MonitoredResources` (the monitored key list), `SecurityAlertsTopicArn` when traceability is on |
| SNS topics | `{prefix}-platform-alarms` (ops), `{prefix}-agentcore-security-alerts` (security) — subscribe endpoints to actually receive anything |

This stack injects nothing into agents; the runtimes emit OTLP spans and
metrics on their own (their role carries the X-Ray and `PutMetricData`
permissions — see [the runtime module](runtime.md)).

## Security notes

- The Transaction Search custom resource's caller policy is broader than the
  two X-Ray actions: `UpdateTraceSegmentDestination` also provisions the span
  log groups and starts Application Signals discovery (service-linked role,
  CloudTrail service-linked channel). The full statement set lives in
  `infra_utils/transaction_search.py`, sourced from the AWS prerequisites page.
- The X-Ray logs resource policy is conditioned on `aws:SourceArn` and
  `aws:SourceAccount` — only X-Ray acting for this account may write spans.
- Neither SNS topic is KMS-encrypted; add a CMK if alert contents are
  sensitive in your environment ([Security controls](../SECURITY_CONTROLS.md)).

## Verification

| Tool | What it proves |
|---|---|
| `scripts/verify.py` | Runs `check_observability.py` always, `check_alarms.py` when `observability.alarms` is on |
| `scripts/check_observability.py` | Trace destination is `CloudWatchLogs` and ACTIVE, span resource policy exists, vended delivery exists per resource |
| `scripts/check_observability.py --spans` | End-to-end: finds a span from the last hour in `aws/spans` and queries it back by traceId (needs a recent invocation; opt-in because delivery lags by a minute or two) |
| `scripts/check_alarms.py` | The `{prefix}-*` alarms exist and none is in `ALARM` state (`INSUFFICIENT_DATA` passes — error metrics only emit on failure) |

## Gotchas

- **Transaction Search is an account- and region-level setting, not
  per-stack.** Deleting this stack deliberately does **not** revert the trace
  destination — other workloads in the account may depend on it by then.
- **The destination update is not idempotent at the API level**: re-running
  it in an account where Transaction Search is already on returns
  `InvalidRequestException` ("already set"). The custom resource ignores that
  error **code** — broader than the message, so a genuinely failed set could
  slip through. `check_observability.py` check 1 is the backstop.
- **Without Transaction Search there are no traces, and nothing tells you.**
  The runtimes emit spans regardless; X-Ray answers every batch with HTTP 400
  while the deploy reports success. This is why the flag defaults on.
- **Empty classic X-Ray API results are not a delivery failure** — with
  Transaction Search only a sample is indexed (Default rule: 1%) while span
  search over `aws/spans` sees 100%. See [Tracing](../TRACING.md).
- **Traceability alerting depends on CloudTrail management events** (the
  security stack); enable them together, and subscribe an endpoint to the
  topic ([`TESTING.md`](../TESTING.md), caveat 5).
- **SNS email subscriptions require clicking the confirmation link** —
  nothing flows until confirmed.
