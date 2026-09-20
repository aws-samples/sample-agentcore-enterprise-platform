# Data Classification and Lifecycle Decision Record

Status: **DRAFT — customer decisions and approvals required**

Gate: G1 — approve the production design

This template records how data moves through an AgentCore accelerator
deployment and how each data set must be stored, protected, retained, and
deleted. It is an accelerator template, not an approved customer policy.
Replace every `[TBD: ...]` value, map the draft classifications to the
customer's policy, and store sensitive evidence in the customer's approved
evidence system.

## Document control

| Field | DRAFT/TBD value |
|---|---|
| Customer / workload | `[TBD: customer and workload name]` |
| Deployment mode | `[TBD: workshop or production]` |
| AWS accounts and Regions | `[TBD: record in approved customer evidence store]` |
| Data owner | `[TBD: named role and person]` |
| Security owner | `[TBD: named role and person]` |
| Privacy / legal reviewer | `[TBD: named role and person, if required]` |
| Version and review date | `[TBD: version and ISO-8601 date]` |
| Approval status | `DRAFT — not approved` |
| Approved record location | `[TBD: customer-controlled evidence location]` |

## Classification scheme

The following levels are a draft starting point. The customer must replace or
map them to its authoritative classification policy.

| Draft level | Suggested interpretation | Example handling expectation |
|---|---|---|
| Public | Approved for public release | Integrity controls; no confidentiality requirement |
| Internal | Non-public operational or business information | Authenticated access and encryption in transit and at rest |
| Confidential | Customer, employee, workload, or business-sensitive data | Least privilege, customer-approved storage, redaction, retention, and audit |
| Restricted | Credentials, regulated data, highly sensitive identity data, or data with severe disclosure impact | Do not collect unless approved; strong isolation, customer-managed controls where required, and verified deletion |

Classification is based on the most sensitive element in a record. Derived
data, embeddings, summaries, logs, traces, backups, and test fixtures inherit
the source classification unless a documented review approves a lower level.

## Data-use boundaries

Complete these decisions before production promotion:

- Approved business purposes: `[TBD]`
- Prohibited data types in prompts or tool calls: `[TBD]`
- Approved user populations and tenants: `[TBD]`
- Approved models, tools, and external processors: `[TBD]`
- Required data residency and cross-border restrictions: `[TBD]`
- Human review requirements for sensitive actions: `[TBD]`
- Whether customer content may be used in evaluation data: `[TBD]`
- Whether production content may be used during an EBA: `[TBD; recommended
  initial position: no]`

## System data-flow register

The rows below seed the review from the accelerator architecture. Confirm each
flow against the synthesized and deployed production profile. Add every
customer tool, model, identity provider, observability sink, and CI/CD system.

| Flow | Data and purpose | Source → processor → destination | Draft classification | Stored? | Required decision / evidence |
|---|---|---|---|---|---|
| DF-01 | User prompt and request metadata for agent invocation | User application → AgentCore Runtime → approved Bedrock model | `DRAFT: Confidential; Restricted if sensitive input is allowed` | `[TBD]` | Allowed content, model Region, logging/redaction, and processor approval |
| DF-02 | Model response returned to the user | Bedrock model → AgentCore Runtime → user application | `DRAFT: inherit prompt and retrieved data` | `[TBD]` | Output filtering, disclosure controls, retention, and user access |
| DF-03 | Tool name, arguments, authorization context, and result | Runtime → AgentCore Gateway → customer or sample tool → Runtime | `DRAFT: inherit highest input/result class` | `[TBD]` | Tool owner, Cedar decision, egress boundary, payload logging, and deletion |
| DF-04 | Session events, summaries, preferences, and semantic memories | Runtime ↔ AgentCore Memory | `DRAFT: Confidential` | `Yes when Memory is enabled` | User/tenant isolation key, KMS key, retention, deletion, backup, and restore |
| DF-05 | Identity claims needed for authentication and authorization | Enterprise IdP / Cognito → Runtime and Gateway | `DRAFT: Confidential` | `[TBD: token and claim handling]` | Minimal claim set, issuer/audience/scope checks, subject mapping, and log exclusion |
| DF-06 | OAuth credentials, client secrets, and short-lived tokens | Secrets Manager / AgentCore Identity → authorized workload | `DRAFT: Restricted` | `Credentials: yes; tokens: transient only unless approved` | Secret owner, rotation, access alarms, token lifetime, and no-log proof |
| DF-07 | Runtime, Gateway, tool, and infrastructure logs | Platform components → CloudWatch Logs / approved archive | `DRAFT: Internal; Confidential if metadata is sensitive` | `Yes` | Field allow-list, redaction, log groups, retention, export, and deletion |
| DF-08 | Correlation identifiers, spans, attributes, and errors | Runtime and dependencies → X-Ray / observability system | `DRAFT: Internal; inherit payload if payload capture is enabled` | `Yes` | Payload capture disabled or approved, sampling, retention, and access |
| DF-09 | Administrative and data-access audit events | AWS and platform control planes → CloudTrail / approved archive | `DRAFT: Confidential` | `Yes` | Trail scope, immutable copy, KMS key, retention, alerting, and reviewers |
| DF-10 | Source, templates, build logs, images, SBOM, and scan evidence | Developer / CI → artifact stores and deployment accounts | `DRAFT: Internal; Restricted if a secret is detected` | `Yes` | Artifact retention, secret quarantine, signing, access, and deletion |
| DF-11 | Configuration names, resource references, and deployment metadata | `platform.yaml` / CI → CloudFormation, SSM, and deployed stacks | `DRAFT: Internal` | `Yes` | No secret values, environment separation, change review, and retention |
| DF-12 | Sanitized release and EBA evidence | Verification systems / facilitator → customer evidence store | `DRAFT: Internal` | `Yes` | Sanitization review, approved destination, retention, access, and disposal |
| DF-13 | EBA design inputs, notes, and participant feedback | Participants / facilitator → workshop systems and handover pack | `DRAFT: Confidential` | `[TBD]` | Consent, approved tools, recording policy, retention, export, and deletion |
| DF-14 | Customer-specific third-party OAuth data, if enabled | Runtime / Gateway → third-party provider | `DRAFT: Confidential or Restricted` | `[TBD]` | Provider review, scopes, token storage, revocation, residency, and contract |

## Data inventory and lifecycle record

Create one record for every stored or transmitted data set. Do not combine
items that have different owners, retention, encryption, Regions, or deletion
paths.

| Field | Required DRAFT/TBD entry |
|---|---|
| Data-set ID and name | `[TBD: stable identifier and descriptive name]` |
| Related flows | `[TBD: DF identifiers]` |
| Business purpose | `[TBD: approved purpose]` |
| Data elements | `[TBD: exact fields; identify free text and derived data]` |
| Data subjects / tenants | `[TBD]` |
| System of record | `[TBD]` |
| Draft classification and policy mapping | `[TBD]` |
| Source and destination | `[TBD: service and trust boundary; keep account IDs out of this repository]` |
| Processor / subprocessor | `[TBD]` |
| AWS Region and residency restriction | `[TBD]` |
| Storage service and resource type | `[TBD]` |
| Encryption in transit | `[TBD: protocol and minimum policy]` |
| Encryption at rest | `[TBD: AWS-owned, AWS-managed, or customer-managed key]` |
| KMS key owner and recovery policy | `[TBD]` |
| Access principals and authorization rule | `[TBD]` |
| Access and deletion audit evidence | `[TBD]` |
| Primary retention period | `[TBD: duration and start event]` |
| Backup / replica retention | `[TBD]` |
| Legal hold behaviour | `[TBD]` |
| Automatic expiry mechanism | `[TBD: service control and configuration reference]` |
| User, tenant, or workload deletion trigger | `[TBD]` |
| Deletion procedure and responsible role | `[TBD]` |
| Maximum deletion completion time | `[TBD]` |
| Deletion verification evidence | `[TBD: query, report, or test; no sensitive payloads]` |
| Restore implications after deletion | `[TBD]` |
| Data owner approval | `DRAFT — [TBD: approver and date]` |
| Security / privacy approval | `DRAFT — [TBD: approver and date]` |

## Retention and deletion schedule

These are decision prompts, not approved defaults.

| Data category | Primary retention | Backup / archive retention | Expiry or deletion mechanism | Deletion test | Owner |
|---|---|---|---|---|---|
| Prompts and responses | `[TBD; recommended initial posture: do not persist raw content unless required]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` |
| Tool arguments and results | `[TBD; recommended initial posture: log metadata, not raw payloads]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` |
| Memory records and derived summaries | `[TBD]` | `[TBD]` | `[TBD: per-user and bulk path]` | `[TBD: cross-user isolation and deletion verification]` | `[TBD]` |
| Runtime and Gateway logs | `[TBD]` | `[TBD]` | `[TBD: log retention / archive lifecycle]` | `[TBD]` | `[TBD]` |
| Traces | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` | `[TBD]` |
| Audit records | `[TBD]` | `[TBD]` | `[TBD: immutable archive lifecycle]` | `[TBD]` | `[TBD]` |
| Identity attributes | `[TBD: minimum necessary]` | `[TBD]` | `[TBD: IdP and platform paths]` | `[TBD]` | `[TBD]` |
| Credentials and tokens | `[TBD: secret versions; tokens should be transient]` | `[TBD]` | `[TBD: rotation, revocation, and deletion]` | `[TBD: retired credential fails]` | `[TBD]` |
| Build and release artifacts | `[TBD]` | `[TBD]` | `[TBD: artifact lifecycle]` | `[TBD]` | `[TBD]` |
| EBA notes and evidence | `[TBD]` | `[TBD]` | `[TBD: handover and facilitator cleanup]` | `[TBD]` | `[TBD]` |

## Encryption and key decisions

| Decision | DRAFT/TBD value |
|---|---|
| Services requiring customer-managed keys | `[TBD from classification and customer policy]` |
| Key-per-environment or key-per-data-domain boundary | `[TBD]` |
| Key administrators and key users | `[TBD: separate roles where required]` |
| Key rotation policy | `[TBD]` |
| Key deletion waiting period and approval | `[TBD]` |
| Multi-Region key requirement | `[TBD from recovery design]` |
| Encrypted log/archive destination | `[TBD]` |
| Behaviour when KMS access is unavailable | `[TBD: expected fail-closed behaviour and runbook]` |
| Evidence | `[TBD: sanitized policy review and live negative test]` |

## EBA handling checklist

- [ ] Participants were told which data may and may not be entered.
- [ ] Synthetic or sanitized data was selected unless customer approval says
      otherwise.
- [ ] Recording, transcription, and AI note-taking decisions were recorded.
- [ ] Facilitator access and emergency access were time-bound.
- [ ] Browser downloads, local files, dashboard artifacts, and shell history
      were included in cleanup.
- [ ] Customer evidence was transferred only to the approved destination.
- [ ] Workshop resources and local artifacts were removed or retained according
      to the agreed handover.
- [ ] Deletion evidence was recorded without copying customer content into this
      repository.

## Validation and approval

| Review | Required evidence | DRAFT decision |
|---|---|---|
| Architecture | Every deployed component and external processor appears in the flow register | `[TBD]` |
| Security | Classification, least privilege, encryption, redaction, and audit controls reviewed | `[TBD]` |
| Privacy / legal | Purpose, residency, retention, deletion, and subprocessors reviewed where applicable | `[TBD]` |
| Operations | Retention jobs, deletion runbooks, backups, and restore behaviour tested | `[TBD]` |
| Data owner | Business use and residual data risk accepted | `[TBD]` |

Approval is not implied by completing the template. Record the authoritative
decision, approver, date, conditions, and evidence location in the customer's
approved governance system.
