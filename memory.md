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

**G1 — production design baseline merged; migration EBA hardening in progress**

Branch: `fix/migration-contract`

G0 merged through PR #74 after prerequisite PR #75. G1 implementation merged
through PR #76. The current branch closes a deployment-account boundary found
during the remaining live evidence run.

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
- No live migration deployment has yet been accepted as evidence. The next
  gate is an isolated rehearsal under a unique project/environment in the
  authorized development account, followed by live invoke, observability,
  rollback, and cleanup evidence.

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
- A real Entra authorization-code sign-in has not been tested because the
  authorized tenant has no `AgentCore Accelerator` app registration.
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
