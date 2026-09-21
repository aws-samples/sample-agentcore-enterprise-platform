# Production Readiness Work Log

This file is the durable handover for the production-readiness work. Update it
when a decision changes, a material risk is found, a milestone is committed, or
new test evidence is produced.

## Current objective

Make the accelerator safe and credible for customer EBA use, then progress
through the release gates in
`docs/PRODUCTION_READINESS_PLAN.md` until the production profile passes an
operational readiness review.

## Current milestone

**v0.1.0 published — customer preflight and honest support boundary**

Branch: `docs/record-v0.1.0-release`

G0 merged through PR #79. G1 implementation merged through PR #76. PR #80
reached `main`, but PRs #81–#86 merged into their stacked base branches rather
than `main`. PR #87 landed the intact final migration stack on `main` on
2026-09-21. PR #88 landed the customer-facing documentation and
documentation-integrity changes on `main` the same day. PR #89 fixed nested
Docsify routes and was merged before the v0.1.0 release-candidate work began.

- [x] Add explicit `workshop` and `production` deployment modes.
- [x] Add a production preset that requires enterprise identity, networking,
  audit, resource policies, egress controls, guardrails, Cedar enforcement,
  traceability, model allow-listing, and monitored alarms.
- [x] Retain production Cognito data, Secrets Manager credentials, Memory,
  KMS keys, CloudTrail storage, ECR images, and platform log groups.
- [x] Make the production trail multi-Region, log-file validated, versioned,
  and KMS-encrypted; apply the CMK to Memory and Gateway.
- [x] Omit Gateway debug exceptions in production.
- [x] Add explicit Memory event and CloudWatch log retention settings.
- [x] Add DRAFT threat-model, data-classification, SLO/RTO/RPO, operating
  model, and risk-register templates without inventing customer approvals.
- [x] Add focused schema and infrastructure synthesis tests.

Open evidence and ownership work:

- [ ] The platform/security owner must record the credential incident and
  attribute the five successful legacy-client token exchanges found in the
  available 90-day CloudTrail history.
- [ ] Complete the Entra authorization-code callback with the account holder's
  physical passkey and retain sanitized result evidence.
- [x] After the 65-minute token drain, run the next locked deployment to remove
  the retired client allow-list/checkpoints and verify the final state.
- [ ] Populate, review, and approve the five G1 governance artifacts for the
  specific customer; draft templates and green synthesis do not close G1.

### 2026-09-21 — v0.1.0 published

- PR #90 merged to `main` as `8f0a7b1` after every required GitHub check
  passed.
- The annotated `v0.1.0` tag resolves exactly to that reviewed merge commit.
- The public GitHub release
  `https://github.com/aws-samples/sample-agentcore-enterprise-platform/releases/tag/v0.1.0`
  was published at 09:14 UTC as a normal release, not a draft or prerelease,
  using the reviewed `docs/releases/v0.1.0.md` notes.
- Tag and release publication changed no AWS resource, secret, customer
  system, or deployed application.
- Follow-up PR
  [#91 — Record v0.1.0 release publication](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/91)
  carries this documentation-only handover record.

### 2026-09-21 — v0.1.0 customer preflight and release candidate

- PR #90 targets `main` with the complete customer-preflight and release
  candidate; it is not stacked on another feature branch.
- `deploy.sh doctor` is a distinct read-only path that runs before normal
  manifest application and deployment setup. It validates the local Python,
  supported Node LTS, npm, AWS CLI v2, Bash, CDK, and optional Docker state;
  the effective manifest; active AWS identity, pinned account, and Region;
  required non-empty Secrets Manager strings; Bedrock model/inference-profile
  metadata; and migration build/image architecture prerequisites.
- The preflight command has an exact AWS read-call allow-list and does not
  install CDK, create or update secrets, bootstrap, synthesize, or deploy. It
  stops account-scoped secret/model reads when the active account does not
  match the manifest. Its early action dispatcher also prevents deployment
  flags from materializing a preset before an invalid doctor invocation is
  rejected.
- A live read-only run found the shell currently resolves to development
  account `…4125`, while the checked-in manifest pins `…1817`. The command
  reported both, exited non-zero, and made no secret or Bedrock call in the
  wrong account. This is accepted fail-closed evidence, not target-account
  secret/model evidence.
- The release candidate adds `VERSION` 0.1.0, a changelog, release/upgrade/
  rollback notes, an evidence-qualified support matrix, and explicit known
  limitations. It does not claim production approval, non-`us-east-1` live
  coverage, or accelerator ownership of customer data/network/trigger/traffic
  changes.
- The `v0.1.0` Git tag and GitHub release must be created only after the
  release-candidate pull request merges, so the tag points to reviewed `main`.
- Evidence passes: 510 repository tests, deployment configuration contracts,
  documentation integrity across 35 Markdown files and 144 local links,
  changed-file Ruff check/format, ShellCheck, shell syntax, and diff
  whitespace. The first sandboxed full-suite attempt was blocked by jsii cache
  permissions; the identical suite passed with normal cache access.
- GitHub checks pass on implementation head `4f1b491`: CodeQL for all
  languages and its aggregate gate, ASH, secret-boundary scanning, contract
  parity, documentation integrity, Python and shell quality, labeler, and both
  control-library jobs.

### 2026-09-21 — migration stack landing and documentation audit

- GitHub's remote graph confirmed that PR #80 reached `main`, while PRs
  #81–#86 merged sideways into their stacked base branches. Their review state
  was complete, but their adapter, verification, networking, data, cutover,
  and documentation commits were absent from `main`.
- PR #87 targets `main` from the intact `feat/migration-cutover-plan` tip. It
  is a landing correction only and performs no AWS or customer-system change.
- The public GitHub Pages site consequently returned 404 for
  `MIGRATION_RUNBOOK`, served an older `platform.yaml` reference, and offered
  no migration navigation.
- The local final-stack documentation renders the runbook successfully at
  desktop and mobile widths. The generated `platform.yaml` tables overflow
  their 390-pixel mobile viewport, however, and need a bounded horizontal
  scroll treatment.
- Follow-up documentation work must add migration navigation, distinguish
  accelerator-managed runtime work from customer-operated data/network/
  trigger/traffic changes, qualify live-verification claims, add a useful 404
  page and favicon, and enforce internal-link/sidebar integrity in CI.

### 2026-09-21 — customer-facing migration documentation remediation

- PR #87 merged and PR #88 was explicitly retargeted to `main`; its diff
  contains only the intended documentation and documentation-integrity files.
- The homepage and workflow guide now describe the implemented migration path
  precisely: compatible arm64 source/image, adapter, AgentCore Runtime, and
  configuration-aware verification. They no longer claim that migration
  automatically extracts tools or state.
- Customer-operated connectivity, data, event-source, traffic, cutover, and
  rollback work is separated from accelerator-managed work. The runbook opens
  with supported/evidence-gated/unsupported scope and accountable roles.
- A dedicated Migrate navigation section links both the EBA runbook and the
  generated migration configuration. The duplicate top-level
  `platform.yaml` link was removed.
- Mobile reference tables now remain inside the 390-pixel document and scroll
  horizontally within their 342-pixel content area. The site also has a local
  favicon and a useful `_404.md`; the expected missing-page request returns
  404 once and the fallback page itself loads successfully.
- `scripts/check_docs.py` and a SHA-pinned GitHub Actions workflow now fail on
  broken local Markdown targets, duplicate or missing required sidebar routes,
  missing local HTML assets, or a missing Docsify not-found page.
- Evidence passes: 504 repository tests; documentation integrity across 32
  Markdown files and 126 local links; Ruff check/format; diff whitespace; and
  browser checks for desktop navigation/search, runbook rendering, mobile
  table containment, favicon loading, and the not-found fallback.
- PR #88's documentation-integrity and label checks pass. The documentation
  workflow intentionally runs on stacked pull requests as well as `main`.
- This branch changes documentation and repository CI only. It performs no AWS,
  customer network, data, event-source, or traffic mutation.

### 2026-09-21 — Docsify project-path fallback correction

- PR #89 contains the focused fallback-path correction.
- GitHub Pages built PR #88 successfully from merge commit `ed45ca3`. Live
  desktop navigation, migration scope, favicon, and mobile table containment
  pass with no console errors on valid routes.
- An unknown live route rendered `_404.md` but then requested
  `/sample-agentcore-enterprise-platform/sample-agentcore-enterprise-platform/_sidebar.md`.
  The same defect reproduced locally when the docs were served below `/docs/`.
- Docsify now receives an explicit base path derived from the current document
  pathname plus a root-sidebar alias for every nested route. The documentation
  integrity checker requires both settings so a future theme/configuration
  edit cannot silently restore the duplicated request.
- The nested-path browser regression now records only the expected 404 for the
  deliberately missing page; `_404.md`, `_sidebar.md`, the favicon, and search
  index pages load from the correct base. Evidence also passes 504 repository
  tests, 32-page/126-link documentation integrity, Ruff, and diff whitespace.

### 2026-09-21 — staged migration readiness and next implementation boundary

- The next migration scope is deliberately split into runtime, networking,
  data, triggers, and traffic. Runtime deployment remains independently usable
  for a target-only EBA rehearsal; every surrounding stage is opt-in.
- `migration.stages` now records `external-copy`, `external-shadow`, and
  `external-canary` only when those customer-operated procedures are in scope.
  `none` is a safe, visible decision and remains the default.
- `migration.network.gate` becomes mandatory for cutover readiness whenever
  private dependencies are declared. A declared VPN or Transit Gateway path
  no longer looks sufficient by itself.
- Every in-scope external stage requires a named owner, separate approver,
  evidence references, rollback procedure, and timezone-qualified approval
  timestamp. `deploy.sh migrate readiness` is configuration-only and exits
  non-zero until traffic is explicitly in scope and every required gate is
  complete. It does not mutate AWS or customer systems.
- Generic data movement was rejected as unsafe: schemas, identity mappings,
  consistency, residency, retention, reconciliation, and reverse replication
  are source-specific. Keep the existing datastore connected during the EBA,
  then add one versioned adapter at a time, starting with a retain-source
  contract and only implementing copy adapters when a customer source is
  known.
- Generic trigger shadowing was also rejected. HTTP/webhook routing can use a
  dedicated authenticated proxy; schedules need idempotent/dry-run behavior;
  and a second consumer on the same queue can steal source work. Future
  trigger PRs must be trigger-specific, disabled by default, and preserve an
  immediate rollback path.
- Evidence passes: 461 repository tests, all 11 deployment-contract
  syntheses, deployment configuration and workshop-flow checks, changed-file
  Python lint/format, ShellCheck, generated-reference drift, shell syntax, and
  diff whitespace validation.

### 2026-09-21 — migration private-dependency probe

- A migration with `network.private_dependencies` now synthesizes a bounded
  Lambda probe in the runtime's private subnets and reuses the runtime security
  group. A migration with no private dependencies creates no probe resource.
- The invocation payload is ignored, so a caller cannot choose an arbitrary
  destination and turn the function into a VPC-side SSRF tool. Up to 25 unique
  manifest hostnames are baked into the function configuration, with a
  3,000-byte aggregate limit below Lambda's environment ceiling.
- Each check performs DNS resolution and a hostname-verified TLS handshake on
  port 443. Results contain only the declared hostname and a fixed status code;
  private addresses, certificate contents, exception text, customer payloads,
  and credentials are not logged or returned.
- An optional private CA bundle is read in memory from the exact declared
  Secrets Manager name through scoped IAM. The function adds a Secrets Manager
  VPC endpoint only when that bundle is configured and never injects the
  bundle into the customer image.
- `check_network.py`, and therefore configuration-aware `verify`, fails if the
  probe is missing, its result host set differs from the manifest, its CA
  cannot be loaded, or any dependency fails DNS/connect/TLS. A green probe is
  runtime-network evidence; the separate owner/approver readiness gate still
  controls cutover.
- Evidence passes: 468 repository tests, all 12 deployment-contract syntheses
  including a private-dependency migration footprint, changed-file Python
  lint/format, generated-reference parity, ShellCheck, and diff whitespace
  checks.

### 2026-09-21 — source-specific migration data plan

- Generic data copy remains intentionally unavailable. `migrate data execute`
  is not a command, and CDK design/build/verify never starts data movement.
- The first versioned data adapter is `retain-source-v1`: the AgentCore
  runtime keeps using an existing customer datastore over a declared and
  verified private dependency. It moves no records and creates no data
  migration role.
- Every retained dataset has a stable name and requires customer evidence
  references for classification, retention, identity mapping, and data
  validation. Its dependency must be one of
  `migration.network.private_dependencies`, so the VPC probe and network gate
  apply.
- `migrate data plan` renders a canonical, sorted, secret-free plan and a
  SHA-256 digest over the project, environment, Region, target, strategy,
  adapter version, dependency, and governance references. Approval fields are
  excluded to avoid a circular digest.
- `migrate data readiness` requires both the data and network gates and
  requires the exact plan digest in `data.gate.evidence`. A configuration edit
  therefore invalidates the prior data approval.
- `external-copy` remains an evidence-only strategy for a separately designed
  customer procedure. Executable copy adapters require an immutable snapshot,
  narrow migration role, encrypted checkpoints, identity mapping,
  reconciliation, and reverse-replication design for the actual source.
- Evidence passes: 472 repository tests, all 12 deployment-contract syntheses,
  deployment configuration checks, changed-file Python lint/format,
  generated-reference parity, ShellCheck, shell syntax, and diff whitespace.

### 2026-09-21 — source-specific migration cutover plans

- Generic event-source or router automation remains intentionally unavailable.
  `migrate cutover plan` and `migrate cutover readiness` are configuration-only
  commands; there is no `execute` action and no AWS or customer-system mutation.
- Runtime readiness is no longer inferred from choosing a supported target. A
  cutover requires the target AWS account, AgentCore Runtime ARN, stack
  `SourceHash`, immutable ECR image digest, retained live-verification
  reference, owner/approver gate, and the exact runtime plan digest. The digest
  also binds the effective identity, agent/model/memory, security, and
  networking-mode configuration.
- Private-network approval is bound to a versioned digest over the runtime
  account, connectivity type, dependency allow-list, DNS forwarders, and
  private-CA secret name plus the deployed VPC, private subnets, security
  groups, probe evidence, and tested CA secret version. VPC/probe/DNS/CA
  changes invalidate the network and downstream traffic approvals.
- HTTP/webhook traffic may use an externally operated percentage canary.
  Webhooks require signature-validation evidence and a read-only or idempotent
  shadow. Schedules require a dry-run/idempotent shadow and an atomic switch.
  Queues require producer dual-publish to a separate destination with
  idempotency evidence and an atomic switch; a competing consumer on the
  source queue is explicitly rejected as unsafe.
- Every traffic plan records its router, source rollback route, target,
  metrics, ordered steps or atomic switch, observation window, and abort
  thresholds. Its digest chains the exact runtime, network, data, and trigger
  plans plus gateway and observability settings. Traffic approval must be
  newer than every required prerequisite approval.
- Operator-provided references reject terminal control characters, numeric
  thresholds reject YAML boolean/string coercion, approval timestamps cannot
  be materially in the future, approvals expire within at most seven days,
  and source/shadow or source/target references must differ.
- Evidence passes: 504 repository tests, all 12 deployment-contract syntheses,
  deployment configuration checks, changed-file Python lint/format,
  generated-reference parity, ShellCheck at warning severity, shell syntax,
  and diff whitespace. No live deployment or external cutover was performed.

### 2026-09-21 — migration profile implementation audit

- The supported migration slice is real: a local Docker build context or
  pre-built arm64 image is built by CodeBuild, wrapped with the migration
  adapter, and deployed to AgentCore Runtime. Focused contract/adapter tests
  and an offline synthesis exercise this path.
- The audit found that `migration.target.runtime: ec2` and
  `migration.target.mode: native` were accepted and documented but never
  consumed by the CDK application. Both paths always produced an adapter on
  AgentCore Runtime. They now fail validation with explicit unsupported-path
  messages instead of silently deploying a different architecture.
- Trigger, VPN/Transit Gateway, and private DNS fields describe customer-side
  discovery and external prerequisites; the accelerator does not provision
  or cut over those systems. The plan and generated reference must state this
  boundary directly.
- The read-only `migrate plan --profile migration` path previously failed on
  the preset's deliberate placeholders and its missing-secret hint put
  plaintext on the command line. Planning now treats placeholders as warnings,
  never overwrites the active manifest, and uses stdin-based secret guidance.
- Follow-on stacked work preserves the source image user after adapter
  installation, replaces lossy comma-separated environment transport with
  JSON, checks child health before every invoke, makes verification migration
  aware, and adds an EBA runbook.
- The isolated live rehearsal is now accepted for the supported fixture path;
  see the evidence below. A real customer source, its acceptance tests, and
  external trigger/network cutover remain customer-specific gates.

### 2026-09-21 — migration adapter/runtime hardening

- The CodeBuild migration path now captures the source image's `Config.User`
  and passes it into the adapter build. Adapter dependencies install as root,
  then the final image restores that source user before startup. The included
  rehearsal image declares numeric uid/gid `10001:10001`, so the live exercise
  proves the non-root path rather than the root fallback.
- Plain migration environment values now travel as a JSON object from the
  manifest through CDK into the child process. Commas and equals signs
  round-trip without corruption; malformed or non-string JSON fails before
  the customer process starts. Declared Secrets Manager values retain final
  precedence.
- The adapter checks both the child process and its declared health endpoint
  before every invocation, using a bounded timeout, and returns a structured
  unavailable response without forwarding when either is unhealthy.
  AgentCore's SDK-owned `/ping` still reports adapter liveness; the runbook
  does not represent it as proof of customer-process health.
- Focused adapter, configuration, and CDK synthesis tests pass. Offline
  synthesis confirms an arm64 source build, source-user capture, the
  `CHILD_USER` build argument, JSON runtime environment, and least-privilege
  migration-secret access.

### 2026-09-21 — migration-aware verification and EBA runbook

- Configuration-aware verification now ignores `agents.pattern` for a
  migration runtime and performs a basic AgentCore invocation against the
  customer image instead of demanding an accelerator-specific Code
  Interpreter tool or AG-UI protocol.
- Transport success is insufficient: the invoke verifier decodes raw JSON or
  JSON SSE events and fails when the adapter reports an application error or
  4xx/5xx-style child status inside an HTTP-success Runtime response.
- `docs/MIGRATION_RUNBOOK.md` defines the supported arm64
  AgentCore-plus-adapter path, isolated rehearsal setup, stdin-only secret
  ingestion, external connectivity/event prerequisites, plan/build/verify
  flow, customer tests, cutover/rollback, cleanup, and an evidence checklist.
- The first isolated rehearsal preflight exposed two design-time UX defects:
  bare YAML `web_search: off` was parsed as boolean and rejected, and a
  schema-valid 34-character project/environment prefix exceeded downstream
  Memory strategy and Logs delivery name limits. Bare `on`/`off` now retain
  their intended enum meaning, and prefixes over 30 characters fail during
  Design with the affected service limits named.
- The same rehearsal showed that the embedded Design view removed the
  migration plan's final line when there were no migration warnings. The plan
  now trims only the blank separator before an actual warnings section and
  retains final environment/prerequisite details.
- The aborted long-prefix run reached only an empty Auth change-set shell in
  `REVIEW_IN_PROGRESS`; it was deleted immediately. No rehearsal resource was
  created and the existing workshop environment was untouched.

### 2026-09-21 — isolated migration live rehearsal

- The rehearsal used a unique `ac-migration/eba` footprint in the authorized
  development account, Cognito identity, no customer secrets, and the included
  non-root EC2-agent fixture. The existing `agentcore-workshop-dev` environment
  was never selected or modified.
- The first runtime build failed before Runtime creation because an unquoted
  buildspec status message contained shell-significant parentheses.
  CloudFormation rolled the isolated Runtime stack back to
  `ROLLBACK_COMPLETE`; the other completed rehearsal stacks remained healthy.
- The status message is now quoted, and a synthesis regression parses every
  generated migration build command with `bash -n`. The retry built the
  arm64 source and adapter images and deployed all six stacks to
  `CREATE_COMPLETE`.
- Live control-plane evidence showed the Runtime `READY`, with
  `MIGRATION_ENV_JSON` present and the legacy `MIGRATION_ENV` channel absent.
  CodeBuild recorded that source uid/gid `10001:10001` was preserved.
- `deploy.sh verify` passed all five checks: brokered identity, Gateway
  discovery/tool invocation, Memory API behavior, active log/trace delivery,
  and a successful migrated-runtime invocation. The strict invoke parser
  accepted the source response rather than only the transport status.
- No real traffic was cut over because the rehearsal used a local fixture and
  no external event source. Failure rollback was exercised by the first build,
  and the successful footprint was then destroyed in dependency order.
  A residue audit confirmed no matching stacks, ECR repository, SSM
  parameters, or Cognito pool. Five service-created test log groups were
  explicitly deleted and a final prefix check returned none.
- That residue exposed a cleanup gap: full destroy did not discover log groups
  created implicitly by CodeBuild and Lambda. Non-production full-footprint
  cleanup now lists exact-prefix service log groups and applies the existing
  ask/`--yes` policy; production mode preserves retained logs.
- Final local evidence: 451 repository tests pass, deployment-config checks
  pass, focused Ruff/formatting and shell syntax pass, and Git diff whitespace
  validation passes.

### 2026-09-20 — live evidence run exposed an unbound deployment account

- The operator selected the intended accelerator-development account, while
  the existing live Entra credential and recent validation environment were
  in a separate development account. The manifest did not declare which
  account it was allowed to modify, so the mismatch was not detected.
- The configured Entra secret and callback did not yet exist for the intended
  accelerator-development account. The deploy script discarded the lookup
  error, prompted for a plaintext replacement, accepted empty input, and
  continued until the Auth stack failed.
- No secret was entered or copied. The intended account's Auth stack reached
  `UPDATE_ROLLBACK_COMPLETE`. The follow-up audit found that only Auth had an
  incident-time update; every consumer stack retained an earlier update
  timestamp, and the owner-checked SSM deployment lock was absent. No
  partial-deployment cleanup was required.
- Root cause: centralized/distributed manifests did not bind the deployment to
  an expected account, and a configured-secret lookup treated missing,
  denied, and wrong-account states as an invitation to create or enter a
  secret.
- Branch `fix/deployment-account-secret-guard` now pins enterprise-IdP and
  production deployments to `deployment.platform_account`, rejects mismatched
  credentials before secret/bootstrap/CloudFormation operations, validates a
  configured SecretString without exposing it, refuses empty interactive
  input, and never replaces a failed configured-secret lookup with a prompt.
- Verification so far: 403 repository tests, deployment-config checks, all
  deployment contract syntheses, targeted Ruff/formatting, shell syntax,
  generated-reference drift, and diff whitespace checks pass.
- A live negative probe with mismatched account credentials was rejected at
  the new preflight before any secret/CDK operation. A non-interactive
  read-only diff against the earlier validation account then validated its
  existing secret without a prompt and confirmed that its pending deployment
  removes the retired client after the completed token drain. The intended
  accelerator-development account requires its own Entra credential and live
  validation.
- After the operator confirmed the intended account, its Cognito callback was
  added to the existing Entra app without removing the earlier callback. A
  dedicated one-year credential was streamed directly into that account's
  Secrets Manager; a non-disclosing check confirmed one current, non-empty
  SecretString.
- The guarded deployment created the Entra provider and updated Auth,
  Identity, Memory, and Gateway. The orchestrator then required a
  generation-based physical replacement, which CloudFormation safely refused
  because Observability still imported its generated Runtime ARN export. The
  orchestrator rolled back completely, its temporary Runtime was deleted, and
  the owner-checked deployment lock was released.
- Root cause: the recovery-only Observability ARN override existed in the CDK
  application and documentation but the normal deploy orchestrator did not
  execute the required handoff. The current evidence branch automates the
  resumable sequence under the deployment lock: pin Observability to the
  current literal ARN at the current Logs source generation, replace the
  Runtime at the next generation, then rebind Observability without the
  override.
- A post-rollback live verification passed all seven checks in the intended
  account: brokered identity, Gateway M2M token and tool call, Memory API,
  trace/log delivery, orchestrator invocation, and both A2A runtimes. The
  partial deployment therefore preserved the customer-facing footprint while
  the automated handoff change is reviewed.
- PR #78 merged the automated runtime/Observability handoff. On the first
  retry, phase 1 completed and released Observability's generated export
  import, but the deploy script stopped because CloudFormation reports an
  existing export with zero consumers as a `ValidationError` instead of an
  empty import list. No runtime replacement had started.
- The import helper now recognizes only CloudFormation's exact no-consumer
  response as an empty result and preserves every other API failure. The
  handoff regression simulates the real non-zero CLI response; deployment
  configuration checks, shell syntax, ShellCheck warnings, and diff whitespace
  checks pass before the resumable live retry.
- The resumable retry completed all eight stacks. The orchestrator
  generation-2 Runtime became active, the old Runtime and generation-1 Logs
  delivery source were deleted, and Observability imported the new generated
  Runtime ARN. All stacks are `UPDATE_COMPLETE`; the owner-checked deployment
  lock is absent and this account has no pending retired-client checkpoints.
- The post-deployment verifier initially reported 7/7 green, but its structured
  orchestrator stream contained an `AccessDeniedException` for
  `StartCodeInterpreterSession`; the model returned a friendly fallback answer
  and `invoke.py` accepted the HTTP success. This is not accepted as clean live
  evidence.
- AWS's Code Interpreter documentation requires session start, invoke, and
  stop permissions. The Runtime role now grants only those three data-plane
  actions on the exact AWS-managed system Code Interpreter ARN. Tool-consuming
  agent patterns now require a structured, successful Code Interpreter result
  containing a seeded marker, so a model response cannot conceal a failed
  dependency.
- Pre-deployment evidence for the correction: 409 repository tests pass,
  deployment-config checks pass, changed Python files pass Ruff and formatting,
  and offline synthesis confirms the three actions are scoped to the system
  Code Interpreter ARN.
- The targeted orchestrator deployment changed only its IAM policy; the
  generation-2 Runtime and image were not replaced. The strict live retry then
  passed all seven footprint checks. Its structured evidence confirms Code
  Interpreter started successfully, executed the seeded Python statement,
  returned the expected marker with exit code zero, and both A2A agents
  remained healthy.

## Decisions

### 2026-09-20 — production work is evidence-gated

Code merge is not sufficient to close a production-readiness item. Each item
needs its acceptance evidence, recorded here or linked from here.

### 2026-09-20 — keep workshop and production modes distinct

The accelerator retains a disposable workshop experience and has a long-lived
production mode. Production mode rejects insecure combinations, retains
stateful/audit resources, uses bounded configurable retention, and is not
accepted by the guided workshop runner.

### 2026-09-20 — generated Cognito secret stays inside the auth stack

The generated Cognito M2M client secret will be copied into Secrets Manager
inside the auth stack. Downstream stacks receive a secret reference, not the
secret-bearing `Fn::GetAtt`. This preserves the existing stack/module boundary
while removing the sensitive CloudFormation export.

The auth stack publishes the secret name through the existing `/auth/*`
interface. Token utilities prefer this reference and retain a narrow
`ParameterNotFound` fallback for deployments created before this change.
Authorization failures do not trigger the fallback.

Existing deployments require a staged upgrade because their identity stack
imports the legacy secret-bearing auth export. The deploy script detects that
export and performs three resumable updates: add the managed secret while
retaining the export, move Identity to the secret-name reference, then remove
the unused export. Direct-mode issuer switches remain on their separate
consumer-first path. The compatibility deployment explicitly retains the safe
secret-name export under a stable, non-sensitive name (CDK references are
otherwise lazy) and redacts the temporary legacy output from CDK's deployment
log.

The legacy credential had already crossed an unsafe boundary, so moving it was
not sufficient. A six-phase protocol creates a versioned M2M client, expands
every inbound authorizer, switches the Identity credential provider, publishes
the replacement, deletes the old client to stop new minting, drains its
already-issued JWTs for 65 minutes, and removes the retired client ID on the
first later deploy. Non-sensitive SSM checkpoints make every phase resumable.
All mutating deployment paths claim an atomic SSM `--no-overwrite` lock before
entering the migration. Lock ownership is checked before release. Existing
lock state is never taken over automatically because SSM cannot conditionally
delete by value; after an uncatchable process termination, an owner must first
confirm that no deployment is active and remove the lock explicitly.

Federated rotation has an additional coordination gate. The platform first
deploys authorizers that accept the old and replacement client IDs, then stops.
Each workload deploys the replacement while temporarily passing the previous
ID as `FEDERATED_RETIRED_M2M_CLIENT_ID`, so both inbound and outbound traffic
remain valid. Only an explicit
`FEDERATED_M2M_CONSUMERS_UPDATED=1` acknowledgement permits old-client
deletion, followed by the same 65-minute JWT drain.

### 2026-09-20 — enterprise federation has one interactive sign-in path

When Entra ID, Okta, or Ping is configured in brokered mode, Cognito public
signup is disabled, the corporate IdP is the sole supported user provider, and
native password/SRP flows are disabled. The public web client uses
Authorization Code flow and is suitable for PKCE. The Cognito-only workshop
path retains its current signup and native authentication experience.

### 2026-09-20 — dashboard data is deny-by-default

The monitor and browser each enforce explicit allow-lists for CloudFormation
outputs and SSM parameters. Poll errors and stale snapshots remove health KPIs
instead of retaining a green view. Status writes are atomic with mode `0600`,
and documented servers bind to `127.0.0.1`. Physical resource IDs are not
serialized. Deployment summaries and `deploy.sh export` reuse the same
reviewed allow-list, with a narrow export-only addition for the federated
replacement client ID and secret-name reference. Exports are locked, atomic,
mode `0600`, and ignored by Git.

### 2026-09-20 — public and internal AI security gates

The public repository needs a portable prompt-injection and data-exfiltration
regression suite. Internal release pipelines can additionally use FAST. Agent
quality will use versioned datasets and explicit release thresholds, with
Amazon Bedrock Evaluations or a framework-neutral evaluator where appropriate.

### 2026-09-20 — secret scanning is a dedicated blocking boundary

The G0 GitHub workflow installs Gitleaks 8.30.1 from a checksum-pinned release,
first proves it detects a synthetic credential, and then scans complete Git
history, synthesized CDK output, and dashboard artifacts. The only allow-list
entry is the conjunction of a generated `cdk.out` path and CDK's exact
content-addressed asset-key shape. Security findings fail the job.

## Resolved G0 risks

- The deployed Cognito M2M credential that appeared in the local dashboard and
  a migration transcript was invalidated. The legacy app client and its stored
  secret were deleted.
- The dashboard now rejects unreviewed CloudFormation outputs and SSM
  parameters in both the collector and browser.
- The M2M secret no longer crosses a CloudFormation export; consumers receive a
  Secrets Manager name and CloudFormation resolves the dynamic reference.
- Enterprise brokered identity no longer offers native Cognito signup/sign-in,
  and the public client no longer uses the implicit grant.

## Remaining high-priority risks

- Default verification does not yet prove end-user federation, agent-level
  memory recall, or searchable trace correlation.
- The live demo runbook records a broken memory demonstration caused by the
  wrong runtime image and silent degradation.
- The real Entra authorization-code callback still needs the account holder's
  physical passkey interaction; redirect and assigned-user challenge evidence
  do not prove a completed sign-in.
- Migration of a real customer image still requires customer acceptance tests,
  load/error-path evidence, and an externally owned network, trigger, cutover,
  and traffic-rollback plan. The accelerator does not provision those systems.
- Production mode, data classification, threat model, retained resources,
  recovery objectives, least-privilege negative tests, and operational
  ownership remain future gates in the production-readiness plan.

Never copy credential values into this file.

## Test evidence

### Baseline review — 2026-09-20

- Unit suite: 340 passed.
- Platform reference parity: passed.
- Deployment config checks: passed.
- Workshop flow checks: passed.
- ShellCheck at warning severity: passed.
- Repository-wide Ruff: 47 existing findings.
- Control-library validation: passed with deep Checkov scan skipped because
  Checkov was not installed.
- No live AWS or Azure validation was performed during the review.

### G0 local implementation — 2026-09-20

- Full unit suite: 383 passed.
- G0 focused auth, identity, token, dashboard, and secret-boundary suite:
  57 passed.
- Ruff on every changed Python file: passed.
- Deployment config checks: passed.
- Workshop flow checks: passed.
- Shell syntax and ShellCheck: passed.
- Platform reference parity: passed.
- All ten deployment-contract synth profiles: passed.
- Control-library validation: passed with the existing warning that Checkov is
  not installed locally.
- `git diff --check`: passed.
- Gitleaks 8.30.1 canary: detected as expected.
- Gitleaks baseline: 142 Git commits, 2.84 MB of generated CDK artifacts, and
  131.12 KB of dashboard assets scanned with no findings.
- A synthetic non-hash credential in a `cdk.out/*.template.json` path was still
  blocked, proving the CDK asset-hash exception does not exclude whole files.
- Browser checks with Playwright: desktop (1280 px) and mobile (390 px) layouts
  fit without horizontal overflow; all tabs worked; no secret reveal/copy
  control or sensitive value was present; stale state removed health data; and
  the final clean session had no console errors.

### Authorized live-test targets — 2026-09-20

- Authorized AWS development account: identity check passed; exact profile and
  account identifiers remain in the approved local evidence.
- Authorized Azure development tenant: identity check passed; the exact tenant
  identifier remains in the approved local evidence.
- Identity checks succeeded for both sessions. Azure application discovery
  returned no registration named `AgentCore Accelerator`; Azure remained
  read-only and no end-user sign-in claim is made.

### G0 AWS migration rehearsal — 2026-09-20

- The first phase created the managed secret and parameter, but the first
  Identity update rolled back because CDK omitted the lazy secret-name export
  from the compatibility template.
- CDK also printed the temporary legacy output after phase 1, exposing the
  already-compromised credential in the local deployment transcript.
- The migration was corrected to publish a stable explicit export containing
  only the secret name and to redact the temporary legacy output. Seeded
  regression tests cover both behaviours.
- The retry completed all three phases: Identity now resolves the Secrets
  Manager reference and Auth removed the legacy secret-bearing export.
- The deploy summary opened repeated AWS CLI pager screens. `deploy.sh` now
  disables the pager for non-interactive, facilitator-friendly output.

### G0 AWS credential rotation and verification — 2026-09-20

- The live rotation completed: replacement created, consumers moved while
  accepting both clients, SSM interface switched, legacy allow-list removed,
  then the old client and secret deleted.
- A lookup of the retired Cognito client returns `ResourceNotFoundException`.
- Auth has no legacy client/secret output, and Identity, Gateway, and Runtime
  contain no reference to the old client ID.
- All consumers import the stable replacement-client reference.
- A replacement M2M JWT was obtained successfully.
- `./scripts/deploy.sh verify` passed all five live checks after legacy
  deletion: brokered identity, Gateway token/tool call, Memory API, trace/log
  delivery, and orchestrator invocation.
- The hardened `deploy.sh export` path was exercised against the authorized
  AWS development account. Its snapshot was mode `0600`, contained only
  reviewed output/parameter keys (including both required V2 federated handoff
  references, but no secret value), passed Gitleaks, and released its SSM
  lock; the temporary local snapshot was then removed.
- A complete 90-day CloudTrail `Token_POST` lookup contained five successful
  exchanges for the deleted legacy client and no failed exchanges. They span
  2026-09-18 14:44:30 UTC through the final pre-rotation verification on
  2026-09-20 08:56:10 UTC. Rotation contains further use; an accountable owner
  must attribute these five events and record the conclusion in the incident
  review.

### G0 Entra live validation — 2026-09-20

- The authorized Azure tenant now has a single-tenant `AgentCore Accelerator`
  app registration with the exact Cognito callback, explicit ownership, v2
  access-token issuance, ID-token issuance, no implicit access-token grant, and
  user assignment restricted to the authorized test user.
- Its one-year credential was streamed directly into the authorized AWS
  development account's Secrets Manager. Only the secret name is configured;
  a non-disclosing check confirmed that the stored value is non-empty and
  whitespace-trimmed.
- The first brokered deployment failed safely before changing Entra identity.
  A prior partial rotation had left Identity on a secret reference while Auth
  was back on its pre-V2 resource shape, with neither safe V2 export nor a
  rotation checkpoint. The migration guard had treated the absent unsafe
  export as sufficient and skipped recreating the replacement.
- The guard now considers migration complete only when the unsafe export is
  absent and both safe V2 handoff exports exist. An unreadable live Auth state
  fails closed, and a regression test pins this partial-retry case.
- The retry completed the three-phase secret migration and deployed the Entra
  provider, then exposed a pre-existing Runtime whose authorizer referenced a
  deleted Cognito pool. AgentCore validated that stale discovery URL even on
  reads and updates, leaving the Runtime stack in `UPDATE_ROLLBACK_FAILED`.
- The stack was restored with only the broken Runtime skipped. A durable
  `agents.orchestrator_runtime_generation` control now performs a
  create-before-delete replacement without replacing healthy A2A runtimes.
  Observability can temporarily pin the old ARN as a literal to release its
  generated CloudFormation import, then uses a generation-specific Logs
  delivery source because that resource also rejects in-place ARN changes.
- Live recovery created a generation-2 Runtime with the current issuer and
  dual-client allow-list, moved the SSM interface, removed the stale
  generation, and rebound log delivery to the replacement. The normal locked
  rotation then moved Identity to the replacement M2M credential, deleted the
  old Cognito client, and entered the 65-minute cached-token drain.
- `deploy.sh verify` passed all seven checks for the live footprint: brokered
  identity, Gateway token/tool call, Memory API, trace/log acceptance,
  orchestrator invocation, and both A2A runtimes. A focused follow-up found
  the observability verifier read only the first paginated Logs API page; it
  now reads every page and reports all five active deliveries.
- Browser validation confirmed Cognito redirects to the exact Entra tenant and
  app callback, and the assigned user reaches the account's FIDO/passkey
  challenge. Completing the authorization-code callback still requires the
  account holder's physical passkey interaction.
- The live local dashboard reports 8/8 deployed and 106 resources. Desktop and
  mobile views have no document-width overflow or console errors; stack
  details, Architecture, and Parameters work, and no M2M/IdP secret reference
  appears in the DOM.
- Design and config read-backs now apply and validate explicit environment
  overrides instead of showing file-only identity values that differ from the
  deployment CDK receives.
- The final local gate passed 390 project tests, 58 secret-boundary tests, all
  preset-to-synth contract checks, deploy-config checks, targeted Ruff and
  formatting checks, shell syntax validation, generated-reference drift, and
  Git diff whitespace validation. GitHub's pinned Gitleaks and ASH jobs remain
  the authoritative scanners after push.
- GitHub validation for commit `c6efa80` passed CodeQL (all languages and the
  aggregate check), ASH, the pinned Gitleaks/secret-boundary gate, deployment
  contract parity, Python and shell quality, and both control-library jobs.
  The only failure is the repository-level Labeler bootstrap described below.

### Independent G0 review corrections — 2026-09-20

- The live rotation did not report an outage, but it had no continuous
  availability probe and one multi-stack phase could update Identity before
  Gateway. The release script now expands Gateway/runtime authorizers in a
  separate phase before Identity can issue replacement-client tokens.
- A second review found that a retry after final deletion failed could
  republish the old SSM interface while authorizers were replacement-only.
  Authorizer expansion now runs before any Auth/SSM mutation, so both the
  happy path and every resumable failure state retain an accepted token path.
- A third review identified cached legacy JWTs as a separate availability
  risk. Both clients now have an explicit 60-minute access-token lifetime.
  Deletion stops old-client minting immediately, while authorizers accept
  already-issued tokens for a 65-minute drain. The first later `deploy.sh` run
  removes the retired client ID using an SSM-backed checkpoint; normal deploys
  during the drain automatically preserve that non-secret allow-list entry.
- A fourth review found that a federated gate stopped before the shared
  Gateway accepted the replacement and that workload runtimes would reject
  cached old-client JWTs. Platform authorizers now expand before the handoff,
  workloads can carry both IDs during the drain, and deletion requires an
  explicit all-workloads-updated acknowledgement.
- A fifth review found that concurrent deploys could enter the same fresh
  rotation and that automatic stale-lock recovery would itself race. Every
  mutating deploy, destroy, and export now uses an atomic owner-checked lock;
  any existing lock fails closed for accountable manual review.
- The same review found an older export utility and deployment summary that
  still collected every CloudFormation output. Both now reuse the dashboard
  allow-list, and seeded regression checks prove the legacy secret-bearing
  output is discarded.
- Compatibility migration now runs before every targeted deployment, because
  CDK can otherwise pull Auth transitively when a Gateway/runtime is selected.
- Invalid `platform.yaml` content now publishes an unavailable dashboard state
  and never falls back to a different default environment.
- The blocking workflow's GitHub Actions are pinned to immutable commit SHAs.
- Invalid or materially future dashboard timestamps fail closed instead of
  appearing live.
- After the concurrency, federated-drain, and export-handoff corrections, the
  independent blocker-only review reported no remaining merge blockers.
- The first pull-request CI run caught a Ruff formatting mismatch and a
  control-library job that ran the full CDK-aware test suite after installing
  only its historical parser dependencies. The formatting was corrected and
  that job now installs the repository's declared requirements before Checkov.
- The new secret-boundary job also exposed that the repository-local
  `cdk.json` expects a developer virtual environment. CI now explicitly invokes
  the runner's installed `python3` and the same credential-free placeholder
  account/region used by the existing contract gate for offline synthesis.
- Clean-checkout CI then caught a test that included an intentionally untracked
  local facilitator runbook. The loopback-only documentation contract now
  covers only the tracked README and participant guide.
- The artifact scan correctly detected seeded credential fixtures when Pytest
  scratch data lived under `dashboard/public`. Test-only scratch data now lives
  in the runner temporary directory, while the deployable dashboard and
  synthesized templates remain fully scanned.
- The failing label job is a repository baseline issue: its workflow references
  a missing `.github/labeler.yml`; recent merged pull requests show the same
  result. Prerequisite PR
  [#75](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/75)
  adds only that file with existing repository labels. Because the workflow
  uses `pull_request_target`, #75 must land on `main` before #74's failed
  Labeler job can be rerun successfully.
- A final check-run audit found that the aggregate CodeQL failure represented a
  new high-severity alert even though every language analysis job passed. The
  alert was an incomplete-URL-sanitization finding on a substring assertion in
  a test; the test now parses the generated JSON and compares the exact
  allow-listed output structure instead.

### G1 production-mode implementation — 2026-09-20

- Added `deployment.mode` with backward-compatible `workshop` behavior and a
  fail-closed `production` control gate. Environment overrides are folded back
  into the typed model before synthesis, and resolved CDK context is checked
  again so direct context flags cannot silently disable a required control.
- Added `presets/production.yaml`. Its real-shaped sentinels are warnings only
  for the offline preset parity gate and hard errors for a real design/build.
  The guided workshop runner explicitly refuses the production profile.
- Production synthesis retains identity data and generated Secrets Manager
  credentials, Memory, KMS keys, CloudTrail data, runtime ECR repositories, and
  platform log groups. The audit bucket is KMS-encrypted, versioned, and
  retained; the trail is multi-Region with log-file validation; Gateway debug
  exceptions are omitted and Gateway uses the platform CMK.
- Added bounded `agents.memory.event_expiry_days` and
  `observability.log_retention_days` settings through schema, environment,
  deployment context, stacks, generated reference, plan output, and tests.
- Added the DRAFT/TBD G1 threat model, data classification, SLO/RTO/RPO,
  operating model, and risk register. These are templates for accountable
  customer review; no approval or launch-readiness claim is inferred.
- Verification evidence: 403 repository tests pass; all eight production
  stacks match the declared contract; changed Python files pass Ruff and
  formatting; shell syntax, generated-reference drift, and diff whitespace
  checks pass. Focused synthesis tests assert retained resources, audit
  hardening, configured retention, production Gateway encryption, and
  sanitized errors.
- No production deployment was performed. The shipped production preset still
  contains deliberate tenant, client, organization, and alarm-destination
  sentinels, so it cannot deploy until a customer replaces and approves them.
- GitHub PR #76 checks passed on implementation head `75cd7e4`: CodeQL, ASH,
  secret-boundary scanning, contract parity, Python quality, shell checks,
  labeler, and both control-library jobs.

## Commits and pull requests

- `3ef164d` — `Harden G0 production-readiness boundaries`
- `c6efa80` — `Recover stale runtimes during identity migration`
- Pull request: [#74 — Harden G0 production-readiness boundaries](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/74)
- Prerequisite pull request:
  [#75 — Add missing pull request labeler configuration](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/75)
- PR #75 and PR #74 merged to `main` on 2026-09-20.
- `05623cf` — `Add production deployment mode and G1 evidence`
- G1 branch: `feat/production-mode-g1`
- Pull request:
  [#76 — Add production deployment mode and G1 design evidence](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/76)
- `efeb2ae` — `Pin deployments to the intended AWS account`
- Pull request:
  [#77 — Pin deployments to the intended AWS account](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/77)
- `b48da8d` — `Automate runtime observability handoff`
- Pull request:
  [#78 — Automate runtime observability handoff](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/78)
- PR #78 merged to `main` on 2026-09-20.
- `3b32ec3` — `Record final live verification evidence`
- Pull request:
  [#79 — Close live runtime verification gaps](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/79)
- PR #79 merged to `main` on 2026-09-20.
- `e81f680` — `Make migration planning fail closed`
- Pull request:
  [#80 — Make migration planning fail closed](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/80)
- `a682dc5` — `Harden the migration adapter runtime`
- Stacked pull request:
  [#81 — Harden the migration adapter runtime](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/81)
- `e6dae9b` — `Add migration verification and EBA runbook`
- `97ea143` — `Fail fast on generated resource name limits`
- `1eaa1bd` — `Fix migration source image build syntax`
- Stacked pull request:
  [#82 — Add migration verification and EBA runbook](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/82)
- Roll-up pull request:
  [#87 — Land migration hardening stack onto main](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/87)
- `52b717a` — `Make migration documentation EBA-ready`
- `bf82e96` — `Run documentation checks on stacked PRs`
- Stacked pull request:
  [#88 — Make migration documentation EBA-ready](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/88)
- `6548ba4` — `Fix Docsify sidebar fallback path`
- Pull request:
  [#89 — Fix Docsify sidebar fallback path](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/89)
- `505be1a` — `Add customer preflight and v0.1.0 release docs`
- Pull request:
  [#90 — Add customer preflight and prepare v0.1.0](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/90)
