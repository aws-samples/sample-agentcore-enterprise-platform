# Production Operating Model

Status: **DRAFT — named owners and customer approvals required**

Gates: G1 ownership design; G4 operational readiness; G6 launch approval

This template defines who owns, operates, secures, changes, and supports a
customer deployment built from the AgentCore accelerator. Roles are separated
from people so the model remains portable. Replace every `[TBD: ...]` with an
approved customer role, named person or group, and durable contact mechanism.

## Document control

| Field | DRAFT/TBD value |
|---|---|
| Customer / service | `[TBD]` |
| Environments and deployment mode | `[TBD]` |
| Service owner | `[TBD: named person and durable role]` |
| Document owner | `[TBD]` |
| Effective and review dates | `[TBD: ISO-8601 dates]` |
| Authoritative record location | `[TBD: customer-controlled system]` |
| Approval status | `DRAFT — not approved` |

## Service scope

| Topic | DRAFT/TBD decision |
|---|---|
| Supported user journeys | `[TBD]` |
| Supported Regions and hours | `[TBD]` |
| Production account and tenancy model | `[TBD: do not put account IDs in this repository]` |
| Platform / application ownership boundary | `[TBD]` |
| Customer-owned tools and dependencies | `[TBD]` |
| AWS or partner support plan | `[TBD]` |
| Service-level objectives | `[TBD: link to approved SLO_RTO_RPO record]` |
| Data-handling rules | `[TBD: link to approved data-classification record]` |
| Known risks and exceptions | `[TBD: link to risk register]` |

## Named roles and contacts

Use durable groups for operational routing and name an accountable individual
for decisions. Do not place private phone numbers, credentials, account IDs, or
customer-confidential contacts in the public repository.

| Role | Named person / group | Responsibilities | Coverage / delegate | Contact or authoritative record |
|---|---|---|---|---|
| Product owner | `[TBD]` | Outcome, scope, claims, funding, and residual-risk acceptance | `[TBD]` | `[TBD]` |
| Service owner | `[TBD]` | End-to-end service health, SLOs, launch readiness, and lifecycle | `[TBD]` | `[TBD]` |
| Platform architect / lead | `[TBD]` | Architecture, production profile, dependencies, and technical decisions | `[TBD]` | `[TBD]` |
| Application / agent owner | `[TBD]` | Agent behaviour, prompts, evaluations, tools, and release quality | `[TBD]` | `[TBD]` |
| Security owner | `[TBD]` | Threat model, IAM, authorization, findings, exceptions, and security response | `[TBD]` | `[TBD]` |
| Identity owner | `[TBD]` | IdP integration, claims, federation, emergency access, and revocation | `[TBD]` | `[TBD]` |
| Data owner | `[TBD]` | Classification, purpose, retention, deletion, and RPO decisions | `[TBD]` | `[TBD]` |
| Privacy / legal reviewer | `[TBD if applicable]` | Privacy, residency, regulatory, and processor review | `[TBD]` | `[TBD]` |
| Operations / SRE lead | `[TBD]` | On-call, alarms, runbooks, incidents, capacity, recovery, and game days | `[TBD]` | `[TBD]` |
| On-call primary and secondary | `[TBD: rotation names]` | Detect, triage, mitigate, communicate, and escalate | `[TBD: 24x7 or business hours]` | `[TBD: paging system]` |
| Release / CI owner | `[TBD]` | Reproducible build, scans, artifact promotion, rollback, and evidence | `[TBD]` | `[TBD]` |
| Test lead | `[TBD]` | Functional, integration, load, negative, and support-matrix evidence | `[TBD]` | `[TBD]` |
| Agent quality owner | `[TBD]` | Evaluation data, thresholds, regressions, and AI safety quality | `[TBD]` | `[TBD]` |
| Customer tool owners | `[TBD per tool]` | Tool contract, authorization, data handling, availability, and rollback | `[TBD]` | `[TBD]` |
| EBA lead / facilitator | `[TBD]` | Qualification, agenda, safe workshop operation, evidence, and handover | `[TBD]` | `[TBD]` |
| Independent launch reviewer | `[TBD]` | Challenge evidence and approve or reject gate completion | `[TBD]` | `[TBD]` |

## On-call and support model

| Decision | DRAFT/TBD value |
|---|---|
| Support hours and time zones | `[TBD]` |
| Primary and secondary rotation | `[TBD]` |
| Paging service and resolver group | `[TBD]` |
| Incident intake channels | `[TBD]` |
| Severity definitions | `[TBD: align with customer incident policy]` |
| Acknowledgement and engagement targets | `[TBD]` |
| Customer communication owner | `[TBD]` |
| Security incident route | `[TBD]` |
| Privacy incident route | `[TBD]` |
| AWS / vendor escalation route | `[TBD]` |
| Handover and shift-change procedure | `[TBD]` |
| On-call access review cadence | `[TBD]` |
| Runbook exercise cadence | `[TBD]` |

### Escalation path

| Trigger | First responder | Technical escalation | Accountable escalation | External escalation | Communication channel |
|---|---|---|---|---|---|
| Service SLO fast burn or total outage | `[TBD]` | `[TBD: operations and platform]` | `[TBD: service owner]` | `[TBD: AWS / dependency owner]` | `[TBD]` |
| Authentication or authorization failure | `[TBD]` | `[TBD: identity and security]` | `[TBD]` | `[TBD: IdP owner]` | `[TBD]` |
| Suspected credential or data exposure | `[TBD]` | `[TBD: security incident team]` | `[TBD]` | `[TBD: privacy/legal and required reporting]` | `[TBD]` |
| Model, prompt, or tool safety event | `[TBD]` | `[TBD: agent quality and security]` | `[TBD]` | `[TBD]` | `[TBD]` |
| Cost anomaly or denial-of-wallet signal | `[TBD]` | `[TBD: platform and finance]` | `[TBD]` | `[TBD]` | `[TBD]` |
| Failed release or rollback | `[TBD]` | `[TBD: release and platform]` | `[TBD]` | `[TBD]` | `[TBD]` |
| EBA blocker or unsafe workshop condition | `[TBD: facilitator]` | `[TBD: EBA lead and domain owner]` | `[TBD]` | `[TBD]` | `[TBD]` |

## Responsibility assignment

R = Responsible, A = Accountable, C = Consulted, I = Informed. Replace role
abbreviations with the customer's approved mapping and ensure each activity
has exactly one accountable role.

| Activity | Product owner | Service owner | Platform | Security | Identity | Data owner | Operations | Agent owner | Release / test | EBA lead |
|---|---|---|---|---|---|---|---|---|---|---|
| Production architecture and profile | C | A | R | C | C | C | C | C | I | I |
| Threat model and security controls | I | C | R | A | C | C | C | C | C | I |
| Data classification, retention, and deletion | C | C | C | C | C | A/R | C | C | I | I |
| Identity claims and federation | I | C | C | C | A/R | C | C | I | C | I |
| SLO, alarms, runbooks, and capacity | I | A | C | C | C | C | R | C | C | I |
| Agent evaluation and AI security gates | C | A | C | C | I | C | C | R | R | I |
| Change approval and production promotion | C | A | R | C | C | I | C | C | R | I |
| Incident command and restoration | I | A | C | C | C | I | R | C | C | I |
| Security incident response | I | C | C | A/R | C | C | C | I | I | I |
| Backup, restore, and recovery exercise | I | A | R | C | C | C | R | I | C | I |
| Risk acceptance and exception expiry | A | C | C | R | C | C | C | C | I | I |
| EBA qualification, execution, and handover | C | C | C | C | C | C | C | C | I | A/R |
| Gate evidence review and launch decision | A | R | C | C | C | C | C | C | C | C |

This matrix is a draft recommendation, not an assignment or approval. Resolve
customer separation-of-duties requirements before adoption.

## Change and release governance

No change is approved solely because its code merged. Promotion requires the
evidence and accountable approval defined for its class.

| Change class | Examples | Required checks | Required approver(s) | Notice / window | Rollback requirement |
|---|---|---|---|---|---|
| Standard / pre-approved | `[TBD: low-risk repeatable changes]` | `[TBD: automated tests and valid pre-approval]` | `[TBD]` | `[TBD]` | `[TBD]` |
| Normal | Agent, tool, dependency, infrastructure, policy, or configuration release | Build, scans, tests, synth/diff, live gates, and risk review | `[TBD: service owner plus domain approver]` | `[TBD]` | Tested last-known-good path |
| High-risk | Identity, authorization, KMS, data lifecycle, network boundary, model allow-list, or destructive change | Normal checks plus security/data review, recovery plan, and independent approval | `[TBD]` | `[TBD]` | Tested and timed rollback or recovery |
| Emergency | Active incident mitigation | `[TBD: minimum safe checks, incident link, and peer review where possible]` | `[TBD: incident commander and emergency change approver]` | Immediate with retrospective | Explicit backout or forward-fix plan |

### Promotion decision record

| Field | Required DRAFT/TBD entry |
|---|---|
| Change and immutable artifact version | `[TBD]` |
| Source review and separation of duties | `[TBD]` |
| Environments receiving the same artifact | `[TBD]` |
| Security, functional, AI, and resilience evidence | `[TBD]` |
| CloudFormation / infrastructure change review | `[TBD]` |
| Data migration and deletion effect | `[TBD]` |
| SLO and error-budget state | `[TBD]` |
| Open risks and exceptions | `[TBD]` |
| Deployment waves and health gates | `[TBD]` |
| Automatic rollback conditions | `[TBD]` |
| Change approver and decision | `DRAFT — [TBD]` |

### Emergency-change requirements

- Tie the change to an active incident and name the incident commander.
- Preserve authentication, authorization, encryption, and audit controls.
- Do not use emergency access to bypass required customer authorization.
- Record commands, scope, evidence, and customer impact in the approved
  incident system.
- Revoke temporary access and rotate exposed credentials.
- Complete peer review, tests, documentation, and a retrospective by
  `[TBD: customer-approved deadline]`.

## Operational routines

| Cadence | Routine | Inputs | Output / evidence | Owner |
|---|---|---|---|---|
| Continuous | Canaries, alarms, audit and cost anomaly detection | Runtime, identity, tools, Memory, traces, security, and spend | Actionable signal and incident when required | `[TBD]` |
| Per shift / business day | Health, queue, failed job, stale telemetry, and open incident review | Operations dashboards | Handover record | `[TBD]` |
| Weekly | Gate, risk, exception, SLO burn, dependency, and delivery review | Release and operating evidence | Decisions, owners, and dates | `[TBD]` |
| Per release | Promotion and rollback readiness review | Immutable artifact and blocking gates | Approval record | `[TBD]` |
| Monthly | Access, secrets, costs, quotas, patching, and SLO report | Security and service reports | Reviewed actions | `[TBD]` |
| Quarterly or approved cadence | Threat model, data lifecycle, supplier, and recovery review | Governance records | Updated decisions and risks | `[TBD]` |
| At least per approved cadence | Game day, restore, rollback, and facilitator rehearsal | Runbooks and staging environment | Measured results and corrective actions | `[TBD]` |

## Runbook and evidence inventory

| Required record | Authoritative location | Owner | Review / exercise cadence | Current status |
|---|---|---|---|---|
| Service overview and dependency map | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Authentication and authorization failure | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Credential exposure and rotation | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Runtime, Gateway, Memory, and tool failure | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| KMS, secret, audit, and observability failure | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Cost anomaly and quota exhaustion | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Deployment rollback | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Backup restore and account / Region recovery | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| Data-subject / tenant deletion | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |
| EBA preflight, fallback, cleanup, and handover | `[TBD]` | `[TBD]` | `[TBD]` | `DRAFT/TBD` |

Sensitive operational evidence stays in the customer's approved evidence
store. This repository should contain only templates, indexes, and sanitized
examples.

## EBA-to-production handover

| Handover item | DRAFT/TBD acceptance |
|---|---|
| Customer qualification and constraints | `[TBD]` |
| Architecture, threat model, and decisions | `[TBD]` |
| Data classification and deletion plan | `[TBD]` |
| Production gap analysis and 90-day roadmap | `[TBD]` |
| Named service, security, data, and operations owners | `[TBD]` |
| On-call, escalation, and support readiness | `[TBD]` |
| Credentials, temporary access, and facilitator access removed | `[TBD]` |
| Customer evidence transferred and local evidence sanitized or deleted | `[TBD]` |
| Workshop resources removed or formally accepted for retention | `[TBD]` |
| Remaining risks accepted in the authoritative register | `[TBD]` |
| Customer acknowledgement | `DRAFT — [TBD: approver and date]` |

## Operating-model approval

| Review | DRAFT decision | Conditions | Named approver and date |
|---|---|---|---|
| Product and service ownership | `[TBD]` | `[TBD]` | `[TBD]` |
| Security and identity ownership | `[TBD]` | `[TBD]` | `[TBD]` |
| Data governance | `[TBD]` | `[TBD]` | `[TBD]` |
| Operations and recovery | `[TBD]` | `[TBD]` | `[TBD]` |
| Change governance | `[TBD]` | `[TBD]` | `[TBD]` |
| EBA handover | `[TBD]` | `[TBD]` | `[TBD]` |

Approval must be recorded in the customer's authoritative system. Names in a
draft template do not grant access, assign an on-call duty, or accept risk.
