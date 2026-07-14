# AgentCore Security Controls — Proposal (for team review)

**Status:** Draft for discussion. No implementation code yet.
**Branch:** `feat/security-controls`
**Author:** (add your name)
**Date:** 2026-07-14

This document proposes how we add reusable, customer-tweakable security controls to the
AgentCore accelerator. It captures the decisions we've already agreed on, the proposed
repo structure, and a phased delivery plan. The goal is to align the team **before** we
write any code.

---

## 1. Goals

Give account teams and customers a set of "getting started" security building blocks for
AgentCore that they can pick, deploy, and tweak for greenfield or brownfield agentic use
cases. Concretely, seven capabilities:

1. **SCPs** for AgentCore services (private-only Runtime, private-only Gateway, CMK for
   Memory, OAuth/JWT-only Runtime, region restriction, mandatory tags, etc.).
2. **Fine-grained VPC endpoint and IAM policies** (least-privilege execution/caller roles,
   AgentCore interface endpoints with endpoint policies).
3. **AgentCore Cedar policies** — default-forbid examples that deny write actions, against a
   sample agent use case.
4. **Resource-based policies** for Runtime, Memory, and Gateway (in-account-only,
   private-only defaults) against sample use cases.
5. **Bedrock Guardrails** for LLM inference, shown across a few agent frameworks and a
   sample agentic use case.
6. **Bedrock Guardrails + AgentCore policy + Lambda interceptor** for egress control — PII
   masking/filtering, prompt-injection defense.
7. **Logging and monitoring** — end-to-end traceability.

---

## 2. Current state of the repo (what we're building on)

`agentcore-accelerator` is a **CDK (Python)** workshop platform, single-account focused,
with a clean modular pattern we want to preserve:

- **Stacks:** `auth`, `identity`, `memory`, `gateway`, `runtime-*`, `observability`, plus
  optional `networking` and `security`.
- **Selection** via `scripts/deploy.sh` using **profiles** (`greenfield`, `migration`,
  `multi-agent`, `platform-team`, `security-focused`), **teams**, and **modules**, driven by
  CDK-context **feature flags** (`enable_networking`, `enable_security`, `enable_a2a`).
- **Cross-stack wiring** through SSM Parameter Store.
- **No Terraform** anywhere today — pure CDK Python.

What exists security-wise is thin:

- `stacks/security_stack.py` — KMS CMK + basic single-region CloudTrail only.
- `stacks/networking_stack.py` — VPC + Bedrock/S3 endpoints, **no endpoint policies**.
- `infra_utils/agentcore_role.py` — a reusable role, but several statements use
  `resources=["*"]`, so **not least-privilege** yet.
- `stacks/gateway_stack.py` — Gateway with CUSTOM_JWT + Lambda targets, **no resource
  policy, no interceptor, no Cedar**.

Most of the seven capabilities are **not built yet**. Items 1 and 7 have raw material
(SCP JSONs and a VPC-lockdown pattern) in the sibling `agentcore-scps` deck repo that we
can seed from.

### Gap map

| # | Capability | Current state | Scope | Engine (initial) |
|---|---|---|---|---|
| 1 | SCPs | None here (10 JSONs exist in `agentcore-scps`) | **Org** | **Terraform + raw policies** |
| 2 | VPCE + fine-grained IAM | VPC + endpoints, no endpoint policies; broad IAM | Account | CDK Python |
| 3 | AgentCore Cedar policies | None | Workload | CDK Python |
| 4 | Resource-based policies | None | Workload | CDK Python |
| 5 | Bedrock Guardrails | None | Workload | CDK Python |
| 6 | Guardrails + Cedar + Lambda interceptor (egress) | None | Workload | CDK Python (feature-flagged) |
| 7 | Logging & monitoring / traceability | KMS + basic CloudTrail; observability stack exists | Account | CDK Python |

---

## 3. Decisions locked in

1. **Option A — scope split.** Terraform owns **org-scope guardrails (SCPs, later RCPs)**.
   CDK (Python) stays the engine for everything **account/workload-scoped**. This matches how
   enterprises actually apply SCPs and avoids doubling maintenance.
2. **SCPs start with Terraform + raw policies.** We ship the raw policy JSON decoupled from
   the apply mechanism, so we can meet customers wherever they manage org guardrails (AFT,
   Control Tower, standalone Terraform, StackSets, or click-ops). Terraform is our reference
   apply path.
3. **Lambda interceptor follows the feature-flag model.** Built in **CDK Python first**,
   toggled by a new `enable_egress_filter` flag exactly like `enable_security`. Terraform
   support for account-scope modules is a later phase.
4. **CDK Python first for account-scope controls; Terraform "soon"** for the same, once the
   pattern is proven.
5. **Shared control folder renamed to `control-library/`** to avoid confusion with AgentCore
   Cedar "policy" or IAM "policy". (Alternatives considered: `security-controls`,
   `control-catalog`, `control-definitions`, `security-baselines`.)

---

## 4. The `control-library/` model

**Single source of truth.** Every control artifact is authored once, in an IaC-agnostic
folder, as **valid JSON / Cedar**. Both Terraform (org guardrails) and CDK (account/workload
controls) read the **same files** and inject parameters at deploy time. Fix a policy once,
both engines get it. No duplicated policy bodies across languages.

Keeping artifacts as **valid JSON** (rather than templated `.tftpl`) lets us run
IAM Access Analyzer, `cfn-guard`, and `checkov` over every file in CI — a real trust signal
for a security accelerator.

```
agentcore-accelerator/
├── control-library/                # ← IaC-agnostic source of truth (raw policies)
│   ├── scp/                         # Service Control Policies (org scope)
│   │   ├── runtime/{private-only,oauth-jwt-only}.json
│   │   ├── gateway/vpce-only-invoke.json
│   │   ├── memory/enforce-cmk.json
│   │   └── org-wide/{region-restriction,mandatory-tags}.json
│   ├── rcp/                         # Resource Control Policies (org scope, later)
│   ├── resource-policies/{runtime,memory,gateway}/*.json
│   ├── iam/{runtime-exec,gateway,caller}/*.json
│   ├── vpce/*.json                  # VPC endpoint policies
│   ├── cedar/<use-case>/*.cedar     # AgentCore Cedar policies (default-forbid on writes)
│   ├── guardrails/<use-case>.json   # Bedrock Guardrails config
│   └── catalog.yaml                 # index + per-artifact param schema + default enforce mode
│
├── terraform/                      # NEW — org-scope guardrails only (Option A)
│   └── org-guardrails/             # reads ../control-library/scp/*.json via templatefile()
│
├── infra_utils/
│   └── policy_loader.py            # NEW — load_control(id, params) -> dict / iam.PolicyDocument
│
├── stacks/                         # EXISTING CDK — consumes account/workload artifacts
│   ├── security_stack.py           # + resource policies; tighten from library
│   ├── networking_stack.py         # + AgentCore VPCEs + endpoint policies
│   ├── gateway_stack.py            # + resource policy + interceptor hook
│   ├── memory_stack.py             # + resource policy
│   ├── observability_stack.py      # + Config rules, CloudTrail data events, EventBridge
│   ├── policy_stack.py             # NEW — Cedar (LOG_ONLY default)
│   ├── guardrails_stack.py         # NEW — Bedrock Guardrails
│   └── egress_interceptor_stack.py # NEW — guardrails + cedar + lambda (items 5+6)
│
└── sample-agents/customer-support-agent/   # fixture for Cedar + guardrails + interceptor
```

> **Note:** `catalog.yaml` describes each artifact: id, file path, type, valid attach points
> (OU / account / resource), required parameters + defaults, and default enforcement mode
> (e.g. Cedar `LOG_ONLY`, Guardrails audit). This is what an account team browses to "pick" a
> control.

### How both engines consume one file

- **Terraform (org guardrails):** `templatefile("${path.module}/../../control-library/scp/memory/enforce-cmk.json", { kms_key_arn_pattern = var.kms_arn_pattern })`.
- **CDK (account/workload):** `policy_loader.load_control("scp.memory.enforce-cmk", {...})`
  reads the JSON, injects params, returns an `iam.PolicyDocument` (or raw dict for L1 props).

---

## 5. Feature-flag integration (native to the existing pattern)

New flags mirror the existing ones in `app.py` and `deploy.sh`:

- `enable_guardrails`, `enable_cedar`, `enable_resource_policies`, `enable_egress_filter`.
- New stacks are conditionally instantiated in `app.py` exactly like `SecurityStack` /
  `NetworkingStack` are today.
- Extend the `security-focused` profile (or add a `regulated` profile) to switch the security
  flags on together:

  ```
  PROFILE_FLAGS[security-focused]="enable_networking=true enable_security=true enable_a2a=false \
      enable_guardrails=true enable_cedar=true enable_resource_policies=true enable_egress_filter=true"
  ```

- **Safe defaults:** Cedar ships `LOG_ONLY`, Guardrails ship in audit posture. Flipping to
  enforce is a flag, not a rewrite.

---

## 6. Lambda interceptor design (items 5 + 6)

- **Toggle:** `enable_egress_filter` feature flag; deployed via `egress_interceptor_stack.py`.
- **Where it hooks:** on the Gateway target path as an interceptor Lambda, inspecting requests
  and responses before egress to tools/targets.
- **What it does:**
  - **PII masking/filtering** and **prompt-injection** checks via Bedrock Guardrails
    (`ApplyGuardrail`), config sourced from `control-library/guardrails/`.
  - **Authorization** via AgentCore Cedar policy (default-forbid on writes) from
    `control-library/cedar/`.
- **Fixture:** the `sample-agents/customer-support-agent` (a couple of read tools + one write
  tool) gives Cedar and the guardrail filters a concrete action set to bind to.
- **Terraform:** deferred; the interceptor is CDK-first per the decision above.

---

## 7. SCPs with Terraform (item 1)

- **Raw policies** live in `control-library/scp/`, seeded from the 10 JSONs in the
  `agentcore-scps` deck repo, then **parameterized** (today they hardcode `vpce-…`,
  `o-yourorgid`, `approved-namespace`, account IDs).
- **Reference apply path:** `terraform/org-guardrails/` reads those JSONs via `templatefile()`
  and attaches to OUs/accounts.
- **Customer deployment models:** because the raw policy is decoupled from the apply
  mechanism, customers can consume the same JSON via AFT, Control Tower, StackSets, or their
  own pipeline. We document these paths; Terraform is the one we ship and test.

---

## 8. Phased delivery plan

**Phase 0 — this document.** Align the team on structure, naming, scope split.

**Phase 1 — vertical slice (prove the model).** CMK-for-Memory (item 1) + Memory
resource policy (item 4):
- Create `control-library/` with `catalog.yaml`, `scp/memory/enforce-cmk.json`,
  `resource-policies/memory/in-account-only.json`.
- Add `infra_utils/policy_loader.py`.
- Wire the resource policy into `memory_stack.py`.
- Add `terraform/org-guardrails/` applying the CMK SCP.
- Add a CI check running `checkov` / Access Analyzer over `control-library/`.

**Phase 2 — egress interceptor (items 5 + 6).** Highest-value piece: Guardrails +
Cedar + Lambda interceptor behind `enable_egress_filter`, with the sample agent fixture.

**Phase 3 — remaining SCPs + fine-grained IAM/VPCE (items 1, 2).** Parameterize the rest of
the SCP set; tighten `agentcore_role.py`; add AgentCore VPCEs + endpoint policies.

**Phase 4 — resource policies for Runtime/Gateway + observability (items 3, 4, 7).** Extend
Config rules, CloudTrail data events, and EventBridge detections from the gateway-lockdown
pattern.

**Phase 5 — Terraform for account-scope modules** ("soon"), once the CDK pattern is proven.

---

## 9. Non-goals (for now)

- Full parallel Terraform tree for every account-scope stack (that was Option B; rejected).
- Changing the existing workshop deployment UX or profile semantics.
- Managing the org management account for the customer — we provide the reference module,
  they choose the apply mechanism.

---

## 10. Open questions for the team

1. **Folder name:** confirmed `control-library/`? (Alternatives listed in §3.5.)
2. **Sample agent:** is `customer-support-agent` the right fixture, or reuse an existing FAST
   reference pattern already in `agent-code/`?
3. **Parameterization syntax:** valid-JSON + `catalog.yaml` param schema (recommended for
   linting) vs. `.tftpl` templates. Agree on valid-JSON?
4. **Profiles:** extend `security-focused`, or add a new `regulated` profile?
5. **Cedar/Guardrails default mode:** confirm `LOG_ONLY` / audit as the shipped default.
6. **CI:** which validators are mandatory in the pipeline (`checkov`, `cfn-guard`, IAM Access
   Analyzer, `cedar validate`)?
7. **Terraform module distribution:** git-tagged modules in this repo, or a separate module
   registry later?
