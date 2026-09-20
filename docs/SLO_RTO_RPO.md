# SLI, SLO, RTO, and RPO Decision Record

Status: **DRAFT — customer decisions and approvals required**

Gates: G1 design decision; G4 measured resilience evidence

This template defines service-level indicators (SLIs), objectives (SLOs),
recovery-time objectives (RTOs), and recovery-point objectives (RPOs) for a
customer workload built on the AgentCore accelerator. Suggested starting
objectives are provisional planning inputs from the production-readiness plan,
not guarantees or approved commitments.

## Document control

| Field | DRAFT/TBD value |
|---|---|
| Customer / service | `[TBD]` |
| User journeys in scope | `[TBD]` |
| Deployment mode and topology | `[TBD: production profile and account model]` |
| Required AWS Regions | `[TBD]` |
| Service owner | `[TBD: named role and person]` |
| Operations owner | `[TBD: named role and person]` |
| Business approver | `[TBD: named role and person]` |
| Measurement start and review cadence | `[TBD]` |
| Approval status | `DRAFT — not approved` |

## Scope and workload assumptions

| Decision input | DRAFT/TBD value |
|---|---|
| Critical user journeys | `[TBD: invoke, tool use, memory recall, or customer-specific journey]` |
| Business hours and time zones | `[TBD]` |
| Forecast average and peak traffic | `[TBD: requests, concurrency, and token volume]` |
| Maximum acceptable request duration | `[TBD]` |
| Planned maintenance treatment | `[TBD]` |
| Invalid request definition | `[TBD]` |
| Valid policy-denial definition | `[TBD]` |
| Dependencies included in the end-to-end objective | `[TBD]` |
| Customer support or contractual commitments | `[TBD: authoritative source]` |

## Recommended initial SLI and SLO set

Every target below remains `DRAFT` until the customer validates the workload,
service quotas, supported Regions, dependency commitments, and measured
baseline. A valid policy block and a correctly rejected invalid request should
not count as a platform failure, but misclassification of either must.

| ID | SLI and measurement | DRAFT recommended initial objective | Window / exclusions | Evidence and owner |
|---|---|---|---|---|
| SLO-01 | End-to-end successful requests / accepted valid requests, measured at the customer-facing boundary | `≥ 99.9%` | `[TBD: monthly rolling window; approved maintenance and valid policy blocks only]` | `[TBD: dashboard, query, and owner]` |
| SLO-02 | Platform-owned tool executions completed correctly / authorized tool executions attempted | `≥ 99.0%` | `[TBD: monthly; separate customer-tool dependency failures]` | `[TBD]` |
| SLO-03 | Accepted requests with a searchable end-to-end correlation trace / accepted requests | `≥ 99.0%` | `[TBD: monthly; define trace-search deadline]` | `[TBD]` |
| SLO-04 | End-to-end latency at p50, p95, and p99, from accepted request to complete response | `[TBD after representative load baseline]` | `[TBD: per use case, model, tool path, and streaming behaviour]` | `[TBD]` |
| SLO-05 | Authentication success / valid interactive sign-in attempts | `[TBD after IdP baseline]` | `[TBD: exclude customer-cancelled and correctly denied attempts]` | `[TBD]` |
| SLO-06 | Memory writes and recalls completed correctly / authorized operations attempted | `[TBD after workload baseline]` | `[TBD: measured separately for write and recall]` | `[TBD]` |
| SLO-07 | Unauthorized model, tool, memory, or cross-tenant access that succeeds | `0` | `Continuous; no exclusion proposed` | `[TBD: audit query, negative tests, and security owner]` |
| SLO-08 | Critical evaluation failures | `0` | `[TBD: per release and scheduled production evaluation]` | `[TBD: versioned evaluation set and quality owner]` |
| SLO-09 | Regression against the approved agent-quality baseline | `No material regression` | `[TBD: define metric, statistical rule, and threshold]` | `[TBD]` |
| SLO-10 | Required canaries reporting fresh data | `[TBD; recommended coverage: auth, invoke, tool, memory, and trace search]` | `[TBD: interval and missing-data threshold]` | `[TBD]` |
| SLO-11 | Requests exceeding the approved cost or token budget | `[TBD]` | `[TBD: per user, tenant, use case, and period]` | `[TBD]` |

### Indicator definitions

For each SLI, attach a query or executable test and complete:

| Field | Required DRAFT/TBD entry |
|---|---|
| Event source and metric namespace | `[TBD]` |
| Numerator | `[TBD: exact event and filters]` |
| Denominator | `[TBD: exact event and filters]` |
| Success and failure codes | `[TBD]` |
| Policy-denial handling | `[TBD]` |
| Missing or delayed telemetry handling | `[TBD; recommended initial posture: alarm, not silently exclude]` |
| Sampling and aggregation | `[TBD]` |
| Dimensions | `[TBD: environment, use case, model, tool, tenant-safe identifier]` |
| Correlation method | `[TBD: non-sensitive correlation identifier]` |
| Dashboard and alarm | `[TBD]` |
| Validation test | `[TBD]` |

## Error-budget decision

For the draft `99.9%` monthly request-success objective, the theoretical error
budget is `0.1%` of accepted valid requests. If represented as time in a
30-day month, that is approximately 43 minutes and 50 seconds; request-based
measurement is preferred for this service. This is a planning illustration,
not an approved outage allowance.

| Decision | DRAFT/TBD value |
|---|---|
| Budget measurement: request-, time-, or journey-based | `[TBD]` |
| Fast-burn and slow-burn alert thresholds | `[TBD]` |
| Action when budget is at risk | `[TBD: release restriction and owner]` |
| Action when budget is exhausted | `[TBD: promotion freeze, exception path, and approver]` |
| Treatment of dependency-caused failures | `[TBD]` |
| Treatment of planned changes | `[TBD]` |
| Reporting cadence and audience | `[TBD]` |

## Dependency objectives

Do not assume an end-to-end objective can exceed the supported design of its
dependencies. Record the selected Region, quotas, customer support plan, and
documented service characteristics for each dependency.

| Dependency | Required capability | Failure effect | Objective / quota evidence | Degradation or fallback | Owner |
|---|---|---|---|---|---|
| Enterprise identity provider / Cognito | Sign-in, token issue, and claim availability | New sessions or all invocations may fail | `[TBD]` | `[TBD: emergency access decision]` | `[TBD]` |
| AgentCore Runtime | Agent invocation | Requests unavailable | `[TBD]` | `[TBD]` | `[TBD]` |
| Amazon Bedrock model / inference profile | Model inference | Requests fail or degrade | `[TBD]` | `[TBD: approved model fallback or fail closed]` | `[TBD]` |
| AgentCore Gateway and customer tools | Authorized tool execution | Tool journeys fail or degrade | `[TBD]` | `[TBD: read-only or no-tool mode, if safe]` | `[TBD]` |
| AgentCore Memory | Session and long-term memory | Stateless operation or request failure | `[TBD]` | `[TBD: fail-closed requirement by use case]` | `[TBD]` |
| KMS and Secrets Manager | Decryption and credential retrieval | Start-up or requests fail | `[TBD]` | `[TBD: no insecure fallback]` | `[TBD]` |
| CloudWatch / X-Ray / audit destination | Detection and evidence | Reduced visibility or release gate failure | `[TBD]` | `[TBD]` | `[TBD]` |
| Customer network and external APIs | Connectivity to users and tools | Partial or total journey failure | `[TBD]` | `[TBD]` | `[TBD]` |

## RTO and RPO decision record

RTO is the maximum approved time from declared disruption to restoration of
the agreed service scope. RPO is the maximum approved age of unrecoverable
data, measured from the disruption. Neither objective is established until a
recovery design demonstrates it and the accountable customer roles approve it.

| Field | DRAFT/TBD value |
|---|---|
| Disruption scenarios in scope | `[TBD: stack failure, account loss, Region impairment, key loss, IdP outage, tool outage, corrupted configuration]` |
| Minimum service to restore | `[TBD: journeys, capacity, and security controls]` |
| Recovery start event | `[TBD: detection or incident declaration]` |
| Proposed RTO | `[TBD: duration per scenario]` |
| Data sets covered by RPO | `[TBD: Memory, audit, configuration, artifacts, and customer data]` |
| Proposed RPO | `[TBD: duration per data set and scenario]` |
| Recovery Region / account strategy | `[TBD]` |
| Backup and replication design | `[TBD]` |
| Infrastructure and immutable artifact source | `[TBD]` |
| Identity and secret recovery | `[TBD]` |
| KMS recovery dependency | `[TBD]` |
| Manual steps and required privileges | `[TBD]` |
| Recovery owner and incident commander | `[TBD]` |
| Last exercise and measured result | `[TBD: date, measured RTO/RPO, and sanitized evidence]` |
| Residual gap / risk-register links | `[TBD: RISK identifiers]` |

### Recovery-scenario decisions

| Scenario | Service and data impact | DRAFT RTO | DRAFT RPO | Recovery strategy | Exercise / evidence |
|---|---|---|---|---|---|
| Failed deployment or configuration | `[TBD]` | `[TBD]` | `[TBD]` | Last-known-good immutable artifact and tested rollback | `[TBD]` |
| Runtime, Gateway, Memory, or tool outage | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD: restore, recreate, or approved degradation]` | `[TBD]` |
| Enterprise IdP outage | `[TBD]` | `[TBD]` | `Not normally data-bearing; confirm` | `[TBD: existing sessions and emergency access policy]` | `[TBD]` |
| KMS key access loss | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD: access recovery; never bypass encryption]` | `[TBD]` |
| Accidental or malicious data deletion | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD: restore without reviving data subject to verified deletion]` | `[TBD]` |
| AWS account loss or compromise | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD: recovery account, artifact, identity, key, and audit plan]` | `[TBD]` |
| Required-Region impairment | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD: same-Region recovery or approved alternate Region]` | `[TBD]` |
| Corrupted model, prompt, policy, or tool release | `[TBD]` | `[TBD]` | `[TBD]` | Roll back versioned configuration and artifact | `[TBD]` |

## Alarm and response mapping

Every approved SLO and recovery dependency needs an actionable alarm or a
documented reason why an alarm is not appropriate.

| SLO / dependency | Signal and threshold | Severity | Missing-data behaviour | First diagnostic query | Immediate mitigation | Runbook | On-call owner |
|---|---|---|---|---|---|---|---|
| `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` |

## Validation plan

- [ ] Representative baseline and peak workload measured.
- [ ] Load test reached twice forecast peak or another approved safety margin.
- [ ] Soak and concurrency tests met each proposed objective.
- [ ] Quotas and pre-alarm thresholds were reviewed.
- [ ] Authentication, invocation, Gateway, Memory, and trace canaries were
      exercised, including missing-data alarms.
- [ ] Retry, timeout, throttling, circuit-breaker, and load-shedding behaviour
      were tested.
- [ ] Deployment rollback restored the last-known-good artifact.
- [ ] Backup restoration and each approved recovery scenario measured RTO and
      RPO.
- [ ] Operations completed a game day using the released runbooks.
- [ ] Results and unresolved gaps were linked to the risk register.

## Decision and approval

| Accountable review | DRAFT decision | Conditions / evidence | Approver and date |
|---|---|---|---|
| Product / business criticality | `[TBD]` | `[TBD]` | `[TBD]` |
| Service owner SLOs | `[TBD]` | `[TBD]` | `[TBD]` |
| Operations supportability | `[TBD]` | `[TBD]` | `[TBD]` |
| Security and recovery controls | `[TBD]` | `[TBD]` | `[TBD]` |
| Data owner RPO | `[TBD]` | `[TBD]` | `[TBD]` |

Gate G1 may record proposed targets; Gate G4 requires measured evidence and
explicit customer approval. Completing this file alone closes neither gate.
