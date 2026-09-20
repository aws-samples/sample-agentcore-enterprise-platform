# Production and EBA Risk Register

Status: **DRAFT — customer ownership, scoring, and approvals required**

Gates: G1 initial register; reviewed at every later production-readiness gate

This template tracks risks for the accelerator platform, customer agent
workloads, delivery pipeline, operations, and EBA experience. Seed entries come
from `PRODUCTION_READINESS_PLAN.md`; they are not findings against every
customer deployment and are not accepted or closed by this document.

Do not store credentials, exploit details, customer identifiers, raw traces,
or sensitive incident evidence here. Link to the customer's access-controlled
record instead.

## Document control

| Field | DRAFT/TBD value |
|---|---|
| Customer / service | `[TBD]` |
| Risk framework / policy | `[TBD: authoritative customer policy]` |
| Accountable product owner | `[TBD: named person]` |
| Security owner | `[TBD: named person]` |
| Review forum and cadence | `[TBD; recommended initial cadence: weekly through launch]` |
| Authoritative register location | `[TBD: customer-controlled system]` |
| Version and review date | `[TBD]` |
| Approval status | `DRAFT — no risk is accepted by this template` |

## Draft scoring method

Replace this scheme if the customer has an authoritative method.

- Likelihood: 1 Rare, 2 Unlikely, 3 Possible, 4 Likely, 5 Almost certain.
- Impact: 1 Negligible, 2 Minor, 3 Moderate, 4 Major, 5 Severe.
- Inherent score: `likelihood × impact` before planned controls.
- Residual score: `likelihood × impact` after implemented and verified
  controls.

| Draft score | Draft rating | Required treatment |
|---|---|---|
| 1–4 | Low | Track and review at the approved cadence |
| 5–9 | Medium | Named owner and dated treatment plan |
| 10–16 | High | Mitigate before launch or record an approved, expiring exception |
| 17–25 | Critical | Proposed initial posture: do not launch until reduced; any exception requires the customer's highest designated authority |

The thresholds and treatment rules are `DRAFT` until customer approval. A
control lowers residual risk only when implementation and operating evidence
exist.

## Required risk record

Every risk and exception must include all fields below. High and critical
deferments are invalid without an owner, expiry, compensating control, and
accountable approver.

| Field | Required entry |
|---|---|
| Risk ID and title | Stable ID and concise event |
| Scope / affected assets | Environment, data, journey, component, or release |
| Risk statement | Cause → uncertain event → business / customer impact |
| Category | Security, privacy, reliability, AI quality, operations, supply chain, cost, compliance, or EBA |
| Evidence / source | Finding, threat scenario, test, incident, or plan item |
| Existing controls | Implemented controls with evidence |
| Inherent likelihood / impact / score | `DRAFT: 1–5 / 1–5 / product` |
| Treatment | Avoid, mitigate, transfer, or accept |
| Planned actions and acceptance criteria | Testable work and completion evidence |
| Required compensating control | Mandatory for deferred high / critical risks |
| Action owner | Named person, not only a team |
| Target date | ISO-8601 date |
| Exception expiry | ISO-8601 date; mandatory for deferred high / critical risks |
| Residual likelihood / impact / score | Supported by verified controls |
| Accountable risk approver | Named person with delegated authority |
| Approval decision and date | DRAFT, approved, rejected, or expired |
| Status | Proposed, open, treating, exception, blocked, or closed |
| Closure evidence | Test / review proving acceptance criteria |
| Next review | ISO-8601 date |
| Related decisions | Threat model, data classification, SLO, change, or incident record |

## Seeded risk register

All scores, owners, dates, statuses, and approvals are intentionally TBD.
Owners must split a row if its components have different treatments or
accountable approvers.

| ID | DRAFT risk statement and source | Category | Inherent L/I | Existing / expected controls | Required treatment and closure evidence | Owner / target / expiry / approver | Residual L/I | Status |
|---|---|---|---|---|---|---|---|---|
| RISK-001 | If credentials or sensitive payloads enter CloudFormation outputs, dashboard artifacts, browser state, logs, CI artifacts, or Git history, unauthorized parties could access customer systems or data. Source: G0 and production definition 1. | Security | `[TBD]` | Secret references, dashboard allow-list, local binding, atomic status writes, secret scanning, credential rotation | Verify generated/deployed outputs and artifacts contain no value; retired credential fails; usage-review conclusion recorded | `[TBD: owner / date / expiry / approver]` | `[TBD]` | `Proposed` |
| RISK-002 | If production authentication permits public local signup, native fallback, implicit grant, or unverified token claims, an attacker could gain or retain unauthorized access. Source: G2 identity. | Security / identity | `[TBD]` | Enterprise federation and JWT authorization exist | Disable unapproved paths; use Authorization Code with PKCE; verify issuer, audience, client, scope, token age, logout, revocation, and emergency access with live tests | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-003 | If broad Cedar, IAM, model, Gateway, or network permissions remain, an agent or caller could invoke unauthorized tools, models, resources, or egress paths. Source: G2 authorization. | Security | `[TBD]` | Model controls, Cedar integration, IAM and optional networking exist | Production requires enforce mode and least privilege; live allowed/denied, modified-claim, cross-account, and direct-bypass tests pass | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-004 | If identity-to-memory partitioning is incorrect, one user or tenant could read another's memory. Source: G2 identity isolation and G3 verification. | Security / privacy | `[TBD]` | Memory and identity claims are integrated | Approve isolation key; run two-real-user cross-user write/read/delete tests; monitor authorization failures | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-005 | If raw prompts, responses, tool payloads, tokens, or sensitive attributes are logged or traced, observability systems could become a data-exfiltration path. Source: G2 data protection. | Security / privacy | `[TBD]` | Vended logs and traces exist | Use structured field allow-lists, redaction, safe sampling, access controls, seeded canaries, and negative scans of live telemetry | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-006 | If Region, retention, encryption, backup, and deletion rules are undefined or untested, customer data may be held or restored contrary to policy. Source: G1 data classification and G2 lifecycle. | Privacy / compliance | `[TBD]` | Service encryption capabilities exist | Approve data inventory; configure lifecycle and keys; test per-user and environment deletion, backup expiry, and restore behaviour | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-007 | If configured Gateway, Memory, identity, guardrail, or tracing dependencies fail open, the runtime could operate without required controls or produce misleading health. Source: G3 fail-closed requirement. | Security / reliability | `[TBD]` | Deployment verification exists | Production validation rejects insecure combinations; dependency failure makes health fail or enters an explicitly approved safe degradation mode | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-008 | If verification tests components only in isolation, advertised end-to-end identity, tool, memory, trace, guardrail, and alarm behaviour may fail for customers. Source: G3 truthful verification. | Quality / reliability | `[TBD]` | A live verification command exists | Test complete deployed user journeys, denials, trace correlation, and fresh alarm metrics; publish only the passing support matrix | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-009 | Without versioned use-case evaluations and adversarial tests, prompt injection, poisoned tool output, excessive agency, harmful output, data exfiltration, or quality regression may reach production. Source: G1 threat scope and G3 AI gates. | AI security / quality | `[TBD]` | Guardrail and tool governance capabilities exist | Approve datasets, rubrics, thresholds, prompt/tool attack cases, and blocking release gates; retain sanitized results | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-010 | Without quotas, token/request budgets, concurrency controls, and cost alarms, accidental loops or denial-of-wallet attacks could exhaust capacity or create material spend. Source: G1 threat scope and G4 cost controls. | Cost / availability | `[TBD]` | AWS quotas and monitoring capabilities exist | Establish per-use-case budgets, bounded retries, limits, anomaly alarms, load shedding, and tested response runbook | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-011 | If production resources, KMS keys, audit records, or state are deleted during stack change/removal, recovery or investigation may be impossible. Source: production mode and G4 recovery. | Reliability / compliance | `[TBD]` | Infrastructure as code exists | Production mode retains protected state and keys; destructive changes require high-risk approval; removal and restore tests preserve agreed evidence | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-012 | If RTO/RPO, backups, rollback, account/Region recovery, IdP outage, and key-loss procedures are unapproved or untested, disruption may exceed business tolerance. Source: G1 and G4. | Resilience | `[TBD]` | Deployment automation and run-time alarms exist | Approve objectives, test last-known-good rollback and recovery scenarios, measure results, and close gaps before launch | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-013 | If dependencies and container bases are unlocked, unsigned, unscanned, or rebuilt between environments, vulnerable or unreviewed software may be promoted. Source: G5 supply chain. | Supply chain | `[TBD]` | CI and image build pipeline exist | Lock with hashes/digests, create SBOM/provenance, scan and sign, block failures, and promote one immutable artifact | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-014 | If security checks can continue on error or findings remain as unowned lint/security debt, releases may proceed with unknown exposure. Source: G5 blocking gates. | Security / governance | `[TBD]` | Repository CI checks exist | Make required checks blocking; move temporary suppressions to expiring exceptions; verify branch protections and independent review | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-015 | If the supported framework, IdP, topology, and Region matrix is broader than tested evidence, customers may select a combination that is not production-ready. Source: G3 support matrix and G6 claims. | Product / reliability | `[TBD]` | Multiple configurable patterns exist | Generate claims from passing jobs; label untested combinations; align README, docs, PRFAQ, and EBA deck | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-016 | If fresh-account deployment, upgrade, rollback, recovery, and removal are not rehearsed, hidden prerequisites and stale resources could block an EBA or production change. Source: production definition 8 and G6. | Operations / EBA | `[TBD]` | Golden-path deployment and verification exist | Run three clean production-like rehearsals, including an independent facilitator; record time, intervention, failure, and correction | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-017 | If the EBA lacks preflight, accessibility, offline/stale-state handling, fallback, cleanup, and handover procedures, participants may be blocked or leave data/access behind. Source: G6 customer experience. | EBA / security | `[TBD]` | Documentation and local dashboard exist | Test with an independent facilitator; complete keyboard/screen-reader review; exercise fallbacks; verify cleanup and customer handover | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-018 | If platform and customer teams lack named owners, on-call coverage, escalation paths, and change approvers, incidents and high-risk changes may have no accountable decision maker. Source: G1 operating model. | Governance / operations | `[TBD]` | Draft role model exists | Approve named roles and RACI; test paging/escalation; enforce change approval and separation of duties | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-019 | If service quotas, required AgentCore/Bedrock features, or identity/tool dependencies do not meet target Regions and traffic, the proposed SLO may be unattainable. Source: G1 SLO and G4 capacity. | Availability | `[TBD]` | Configurable Region and service monitoring exist | Validate regional support and quotas; load/soak at approved margin; alarm before limits; record dependency objectives and fallback | `[TBD]` | `[TBD]` | `Proposed` |
| RISK-020 | If third-party OAuth providers or customer tools receive excessive scopes or data, a compromised dependency could act as a confused deputy or exfiltrate data. Source: G1 threat scope and G2 authorization. | Security / third party | `[TBD]` | Gateway and identity integrations support governed access | Inventory processors and scopes; approve contracts/residency; isolate credentials; enforce egress and tool policy; test revocation and poisoned responses | `[TBD]` | `[TBD]` | `Proposed` |

## Exception / risk-acceptance record

Copy this block for any risk proposed for temporary acceptance. An exception is
invalid after its expiry or when its assumptions change.

| Field | Required DRAFT/TBD entry |
|---|---|
| Risk ID | `[TBD]` |
| Scope and release | `[TBD]` |
| Business reason mitigation cannot complete before release | `[TBD]` |
| Residual impact and affected users / data | `[TBD]` |
| Compensating control and monitoring | `[TBD: required for high / critical]` |
| Control owner and proof it operates | `[TBD]` |
| Conditions that trigger immediate rollback / revocation | `[TBD]` |
| Remediation owner and dated plan | `[TBD]` |
| Start date and non-renewing expiry | `[TBD: ISO-8601 dates]` |
| Accountable approver with delegated authority | `[TBD]` |
| Security / data / operations concurrence | `[TBD as applicable]` |
| Decision and date | `DRAFT — not accepted` |

## Review checklist

- [ ] Every production-readiness finding and threat-model scenario has a risk,
      control, or documented not-applicable rationale.
- [ ] Every risk has a named action owner, target date, and next review date.
- [ ] Inherent and residual scores follow the approved customer method.
- [ ] Controls cited as existing have current evidence.
- [ ] Every deferred high or critical risk has an unexpired compensating
      control and accountable approver.
- [ ] Expired exceptions block promotion until renewed through the approved
      process or remediated.
- [ ] Closed risks include evidence and were reviewed by the accountable role.
- [ ] Repository links are sanitized; sensitive evidence remains in the
      customer-controlled store.
- [ ] Product and EBA claims disclose material residual limitations.

## Gate review

| Gate | Review question | DRAFT decision / evidence |
|---|---|---|
| G1 | Are production-design risks identified, scored, owned, and approved for treatment? | `[TBD]` |
| G2 | Do live identity, authorization, data, and network tests reduce the expected risks? | `[TBD]` |
| G3 | Do functional, AI quality, and AI security gates cover the accepted threat scenarios? | `[TBD]` |
| G4 | Do load, recovery, rollback, and game-day results meet approved objectives? | `[TBD]` |
| G5 | Are supply-chain controls blocking and exceptions current? | `[TBD]` |
| G6 | Are residual risks approved and accurately represented in customer materials? | `[TBD]` |

Gate completion requires evidence and review in the authoritative customer
system. A repository checkbox or merged pull request is not risk acceptance.
