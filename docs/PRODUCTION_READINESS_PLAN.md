# Production Readiness Plan

Status: in execution — G0 code merged; G1 implementation in progress

Planning horizon: 12 weeks

Scope: the AgentCore platform accelerator, its delivery pipeline, and the EBA
customer experience

## Outcome

At the end of this plan, the accelerator can be used as a production baseline
with evidence that its security, identity, agent behaviour, deployment,
observability, and recovery controls work.

The accelerator cannot certify every customer application as production-ready.
Each customer must still approve its data classification, threat model, SLOs,
AI quality thresholds, compliance controls, and disaster-recovery objectives.
The accelerator is ready when it makes those decisions explicit, deploys safe
defaults, tests the selected controls, and produces the evidence needed for a
customer launch review.

## Production definition of done

A release is production-ready only when all of these statements are true:

1. No credential or sensitive payload appears in CloudFormation outputs,
   generated templates, dashboard files, browser state, logs, or CI artifacts.
2. Production identity uses the customer's IdP, disables public local signup,
   and verifies a real end-user sign-in and authorization flow.
3. IAM, model access, tool access, network access, and encryption controls are
   enforced by default and tested against the live deployment.
4. Agent quality and AI security regressions have measurable release
   thresholds.
5. Every advertised capability is tested through the deployed agent, including
   identity, tools, memory, traces, guardrails, and alarms.
6. The platform has approved SLOs, alarms, runbooks, rollback, backup, and
   recovery procedures.
7. Builds are reproducible, dependencies are locked, artifacts are scanned,
   and CI failures block promotion.
8. A fresh-account deployment, upgrade, rollback, and removal have passed in a
   production-like staging environment.
9. The PRFAQ, deck, documentation, and delivered features make the same claims.
10. Security, platform engineering, operations, and the product owner have
    approved the release evidence.

## Delivery model

Use two explicit deployment modes:

| Mode | Purpose | Resource lifecycle | Security posture |
|---|---|---|---|
| `workshop` | Disposable EBA and learning environment | Fast creation and clean removal | Safe for customer exercises, with clearly documented limitations |
| `production` | Long-lived customer platform | Retained data, controlled changes, backups, and recovery | Secure defaults; disabling a required control needs an approved exception |

`deployment.mode` is part of `platform.yaml`, and `presets/production.yaml` is
the secure starting point. The production preset becomes the target of every
release gate. Existing workshop profiles remain useful but must not be used as
evidence that the production profile is ready.

## Roadmap

The phases overlap where the work is independent. A gate closes only when its
evidence is stored with the release.

| Phase | Target | Primary result | Exit gate |
|---|---|---|---|
| 0. Contain immediate risks | Days 0–2 | Known credential and dashboard exposure removed | G0 |
| 1. Approve the production design | Weeks 1–2 | Threat model, data rules, production profile, and ownership approved | G1 |
| 2. Harden identity and security | Weeks 2–4 | Secrets, authentication, authorization, IAM, network, and logging fixed | G2 |
| 3. Make verification truthful | Weeks 3–6 | End-to-end functional, AI quality, and AI security gates | G3 |
| 4. Engineer operations and resilience | Weeks 5–8 | SLOs, alarms, runbooks, rollback, recovery, load, and cost controls | G4 |
| 5. Harden the software supply chain | Weeks 7–10 | Reproducible builds and blocking CI/CD promotion | G5 |
| 6. Rehearse and approve launch | Weeks 10–12 | Fresh staging rehearsal, ORR, customer kit, and signed release | G6 |

## Phase 0 — contain immediate risks

Owner roles: Security lead and platform lead.

| ID | Work | Acceptance evidence |
|---|---|---|
| PR-001 | Recreate the exposed Cognito M2M app client and invalidate the old client. Review recent use of the client before closing the incident. | New client works; old client fails; incident record and review completed |
| PR-002 | Remove the client secret from cross-stack CloudFormation outputs. Co-locate its consumer or store it directly in Secrets Manager and pass only a reference. | Synthesized templates and deployed stack outputs contain no secret value |
| PR-003 | Change the dashboard collector from collecting every output to an explicit safe-field allow-list. Remove secret reveal/copy behaviour. | Automated test injects secret-shaped outputs and proves none reach `status.json` or the DOM |
| PR-004 | Bind the documented dashboard server to `127.0.0.1`, restrict the status file to the local user, and write it atomically. | Another host cannot connect; interrupted writes cannot produce partial JSON |
| PR-005 | Add secret scanning for source, generated templates, dashboard artifacts, and Git history. | CI blocks seeded test credentials and produces a clean baseline report |

Gate G0 closes when the secret is rotated, the old credential is unusable, the
dashboard cannot publish secrets, and the regression tests are blocking.

Current progress (2026-09-20): PR-002 through PR-005 are merged, and the
technical part of PR-001 is complete: the deployed
legacy client and credential were deleted after a verified, consumer-first
rotation. The blocking workflow is merged. Formal closure still requires the
platform/security owner to record the incident and usage-review limitation.
Detailed, non-sensitive evidence is maintained in the repository-root
`memory.md` work log.

## Phase 1 — approve the production design

Owner roles: Product owner, platform architect, security lead, operations lead,
and customer identity representative.

1. Create a threat model covering user-to-agent, agent-to-model,
   agent-to-tool, memory, third-party OAuth, CI/CD, administrators, and the EBA
   facilitator path. Include prompt injection, confused deputy, excessive
   agency, data exfiltration, cross-tenant access, poisoned tool output, and
   denial-of-wallet scenarios.
2. Produce a data-flow and classification document. Define which prompts,
   responses, tool arguments, memory records, traces, and identity claims may be
   stored, their Region, retention period, encryption key, and deletion path.
3. Decide and document availability, latency, recovery, and data-loss targets.
   Confirm that the selected AgentCore and Bedrock features can meet them in
   every required Region.
4. Add `workshop` and `production` deployment modes. In production mode:
   retain stateful resources, protect encryption keys, use the approved audit
   trail, enable alarms, and reject insecure combinations during config
   validation.
5. Define the support model: named service owner, security owner, on-call
   rotation, escalation path, dependency owners, and change approvers.
6. Create a risk register. Every deferred high or critical finding needs an
   owner, expiry date, compensating control, and accountable approver.

Gate G1 evidence: approved threat model, data classification, target SLO/RTO/RPO
document, ownership map, risk register, and a synthesized production profile.

Current progress (2026-09-20): the production mode, secure validation gate,
retained-resource behavior, configurable retention, and production preset are
implemented on the G1 branch with synthesis tests. The five governance
artifacts exist as explicit DRAFT/TBD templates. They require customer-specific
values, evidence, and accountable approvals before G1 can close.

## Phase 2 — harden identity and security

Owner roles: Identity engineer, security engineer, and platform engineer.

### Identity and secrets

- Disable Cognito self-signup and native Cognito sign-in when an enterprise IdP
  is selected. Keep any emergency local access admin-created, monitored, and
  explicitly enabled.
- Use Authorization Code with PKCE for public browser clients. Remove implicit
  grant.
- Verify issuer, audience, client, scopes, token age, and the identity claim
  used for Memory isolation. Document logout, revocation, token renewal, and
  emergency access.
- Add automated sign-in tests against each supported IdP mode. The test must
  invoke a runtime as an end user and prove that another user cannot read its
  memory.
- Put every credential in Secrets Manager with rotation ownership and alarms
  for failed retrieval or rotation. Configuration carries names or ARNs only.

### Authorization and least privilege

- Replace the general Cedar permit with use-case-specific principal, action,
  tool, and resource rules. Production mode requires `ENFORCE`.
- Test allowed and denied tool calls through the live Gateway. Test privilege
  escalation, modified claims, missing scopes, cross-account callers, and
  direct calls that bypass the agent.
- Scope runtime IAM to approved models or application inference profiles,
  ECR repositories, log groups, KMS keys, parameters, memories, gateways, and
  credential providers.
- Set Gateway exception reporting to a production-safe level. Return stable
  client error codes while retaining redacted diagnostic context in protected
  logs.
- Enforce private connectivity and controlled egress where the customer's data
  classification requires it. Validate DNS, endpoint policies, and failure
  behaviour.

### Data protection and audit

- Remove raw prompt, response, tool payload, token, and event logging. Use a
  shared structured logger with field allow-lists, redaction, correlation IDs,
  and configurable sampling.
- Define production retention and deletion for CloudWatch logs, traces, Memory,
  audit data, and build artifacts.
- Use retained KMS keys and an approved multi-Region or organization audit
  trail. Send immutable audit copies to the customer's log archive when
  required.

Gate G2 closes when identity isolation, authorization denials, least-privilege
policies, private access, redaction, retention, and audit delivery all pass
live negative tests.

## Phase 3 — make verification truthful

Owner roles: Test lead, agent quality owner, security test owner, and framework
maintainers.

1. Change `verify` so it tests capabilities through the deployed runtime:

   - complete an end-user authentication flow;
   - call an allowed tool and reject a forbidden tool;
   - store and recall memory across separate invocations;
   - find the invocation's trace by correlation ID;
   - prove guardrail and model allow-list denials;
   - create traffic and prove each required alarm receives fresh metrics.

2. Make required dependencies fail closed. A production runtime must report
   unhealthy when configured Gateway, Memory, identity, guardrail, or tracing
   initialization fails.
3. Replace weak memory assertions with behavioural assertions: created events
   must be returned, pagination limits and tokens must be correct, and only the
   expected service error counts as a valid negative result.
4. Build an evaluation dataset for every production use case. Version its
   prompts, expected tool choices, policy outcomes, and scoring rubric. Gate
   releases on task success, tool correctness, groundedness where relevant,
   harmful-content policy, and regression from the approved baseline.
5. Add adversarial AI tests for prompt injection, indirect injection, tool
   misuse, data exfiltration, sensitive-data disclosure, and resource
   exhaustion. FAST can provide the internal pipeline gate; the public sample
   also needs a portable regression suite that customers can run.
6. Run the complete matrix for all advertised framework patterns, supported
   identity modes, deployment topologies, and target Regions. Publish the exact
   tested matrix instead of making universal framework claims.
7. Add load, soak, concurrency, quota, throttling, timeout, retry, malformed
   event, and dependency-failure tests.

Gate G3 evidence: versioned evaluation results, AI security results, full live
verification, and a support matrix generated from passing jobs.

## Phase 4 — engineer operations and resilience

Owner roles: SRE/operations lead and platform engineer.

Recommended starting objectives must be reviewed against the customer use case:

| Measure | Initial target |
|---|---|
| End-to-end successful request rate | At least 99.9% monthly, excluding valid policy blocks and invalid client requests |
| Tool execution success | At least 99% for platform-owned tools |
| Trace coverage | At least 99% of accepted requests have a searchable correlation trace |
| Unauthorized model or tool use | 0 |
| Quality regression | No critical evaluation failure and no material regression from the approved baseline |
| Recovery | Customer-approved RTO and RPO, demonstrated in a recovery exercise |

Complete these operational controls:

- Measure baseline and peak traffic, then test at least twice the forecast peak
  or the agreed safety margin.
- Add canaries for authentication, runtime invocation, Gateway tool calls,
  Memory recall, and trace search. Alarm on missing data as well as bad data.
- Give every alarm an owner, severity, customer impact, first diagnostic query,
  mitigation, rollback step, and escalation path.
- Add dependency dashboards for Bedrock, AgentCore, Cognito/IdP, Gateway tools,
  KMS, Secrets Manager, and CloudWatch delivery.
- Implement bounded retries with jitter, idempotency where actions can repeat,
  concurrency limits, request budgets, and circuit-breaking or load shedding.
- Define deployment waves, health gates, automatic rollback conditions, and a
  tested last-known-good release.
- Test backup, restore, Region or account recovery, key access loss, IdP
  outage, tool outage, and corrupted configuration. Record measured recovery
  times.
- Use application inference profiles or equivalent attribution, resource tags,
  budgets, anomaly alarms, per-use-case token metrics, and denial-of-wallet
  limits.
- Review service quotas before promotion and alarm before reaching operational
  limits.

Gate G4 closes after the load test, soak test, failure exercises, rollback, and
recovery exercise meet the approved objectives and the on-call team completes
a game day.

## Phase 5 — harden the software supply chain

Owner roles: Release engineer, security engineer, and repository maintainers.

- Lock Python and Node dependencies, including transitive versions and hashes.
  Commit lock files and use them for local, CI, CodeBuild, and container builds.
- Pin container base images and build tools by immutable digest. Remove
  unauthenticated remote setup scripts from builds or verify downloaded
  artifacts cryptographically.
- Generate an SBOM for every image and release. Scan source, IaC, dependencies,
  licenses, containers, and secrets. Sign images and attach provenance.
- Make repository-wide lint, unit tests, CDK synth, Terraform validation,
  Checkov, secret scanning, SAST/SCA, image scanning, live integration tests,
  AI security tests, and evaluation thresholds blocking.
- Remove `continue-on-error` from security gates. Approved exceptions live in
  the risk register with expiry dates.
- Clear the existing lint backlog and prevent new debt.
- Protect release branches, require reviewed changes, separate deployment
  approval from authorship, and restrict production roles.
- Version the accelerator and its schema. Publish release notes, tested
  combinations, migration instructions, known limitations, and rollback
  instructions.
- Promote one immutable artifact through dev, staging, and production. Do not
  rebuild between environments.

Gate G5 evidence: reproducible rebuild, clean blocking pipeline, signed images,
SBOM, vulnerability report, provenance, and a staged promotion of the same
artifact.

## Phase 6 — rehearse and approve launch

Owner roles: Product owner, EBA lead, security lead, operations lead, and an
independent reviewer.

1. Align the PRFAQ and deck with the shipped support matrix and evidence.
   Describe the EBA as producing a production baseline and customer roadmap
   unless the customer's own launch gates have also passed.
2. Deliver the missing customer artifacts:

   - qualification and intake form;
   - security and data discovery questionnaire;
   - preflight checklist;
   - facilitator runbook with timings and fallbacks;
   - architecture decision and threat-model templates;
   - requirements and product-signal capture;
   - gap analysis, risk register, and 90-day roadmap;
   - customer handover, operations, and launch-readiness checklist.

3. Retire or clearly archive conflicting workshop scripts and keep one
   documented golden path.
4. Test the CLI, documentation, dashboard, and deck with a facilitator who did
   not build the project. Complete keyboard, screen-reader, error-message, and
   stale/offline-state checks.
5. From a fresh staging account, execute qualification, design, deployment,
   end-user federation, all verification, upgrade, rollback, recovery, and
   removal using only released documentation.
6. Repeat the clean staging rehearsal three times, including one run by an
   independent facilitator. Record duration, manual interventions, failures,
   and corrective actions.
7. Hold an operational readiness review. Security, operations, platform
   engineering, and product sign the evidence and remaining risks.

Gate G6 closes when all earlier gates remain green, the three rehearsals pass,
the customer kit is complete, and the operational readiness review is signed.

## Required release evidence

Store a versioned, non-sensitive evidence index for each release:

| Evidence | Accountable role |
|---|---|
| Architecture, data flow, threat model, and security decisions | Security lead |
| IAM and authorization review, negative-test results | Security lead |
| Dependency locks, SBOM, scans, signatures, and provenance | Release engineer |
| Functional, framework, identity, and topology matrix | Test lead |
| Agent evaluation and AI security reports | Agent quality owner |
| Load, soak, quota, game-day, rollback, and recovery reports | Operations lead |
| SLOs, dashboards, alarms, and runbooks | Service owner |
| Customer kit and claims-to-evidence matrix | Product/EBA owner |
| Risk register and final approvals | Product owner |

Evidence containing customer data, credentials, account identifiers, or
security-sensitive traces must remain in the customer's approved evidence
store. The repository should contain only the index, templates, and sanitized
examples.

## First ten implementation tickets

Start in this order:

1. PR-001 — rotate the M2M client and review its use.
2. PR-002 — eliminate the cross-stack secret output.
3. PR-003/004 — sanitize and localize the dashboard.
4. Add `deployment.mode` and `presets/production.yaml`.
5. Harden Cognito and enterprise IdP flows; remove implicit grant.
6. Make configured Gateway and Memory failures fail the runtime health check.
7. Add real user, tool, memory, trace, and alarm checks to `verify`.
8. Add the use-case evaluation harness and initial AI security regression set.
9. Make security scanning blocking and add dependency/image locking.
10. Complete one fresh-account staging rehearsal and use its failures to plan
    the remaining work.

## Program tracking

Run one weekly gate review led by the product owner. Track each item with an
owner, due date, dependency, current evidence, and blocker. A percentage-complete
score is not a launch signal; the current gate and its unmet acceptance criteria
are the launch signal.

Do not close a task because code merged. Close it when the acceptance evidence
exists and the accountable role has reviewed it.
