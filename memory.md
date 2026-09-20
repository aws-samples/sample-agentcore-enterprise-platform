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

**G0 — contain immediate risks (implementation ready for review)**

Branch: `security/production-readiness-g0`

Completed on this branch:

- [x] Remove the Cognito M2M client secret from cross-stack CloudFormation
  outputs.
- [x] Prevent the local dashboard from collecting, serving, or displaying
  secrets.
- [x] Harden brokered enterprise identity so native Cognito signup cannot
  bypass the corporate IdP.
- [x] Preserve the Cognito-only workshop path.
- [x] Add a blocking secret-boundary workflow covering Git history,
  synthesized templates, and generated dashboard artifacts.
- [x] Deploy and verify the migration in the authorized AWS development
  account.
- [x] Rotate the deployed Cognito M2M client and delete the exposed legacy
  client and secret.
- [x] Serialize mutating deployments and exports with an owner-checked lock;
  fail closed on abandoned lock state.
- [x] Apply the dashboard public-data allow-list to deployment summaries and
  exported workshop artifacts.
- [x] Verify the authorized Azure session and check for a test app
  registration. No matching registration exists, so an Entra browser sign-in
  was not possible in this milestone.

Before G0 is formally closed:

- [ ] Merge the blocking CI regression gate.
- [ ] The platform/security owner must record the credential incident and
  attribute the five successful legacy-client token exchanges found in the
  available 90-day CloudTrail history.

## Decisions

### 2026-09-20 — production work is evidence-gated

Code merge is not sufficient to close a production-readiness item. Each item
needs its acceptance evidence, recorded here or linked from here.

### 2026-09-20 — keep workshop and production modes distinct

The accelerator will retain a disposable workshop experience and add a
long-lived production mode. Production mode will reject insecure combinations
and retain stateful/audit resources. This is planned after G0 containment.

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

## Commits and pull request

- `3ef164d` — `Harden G0 production-readiness boundaries`
- `c6efa80` — `Recover stale runtimes during identity migration`
- Pull request: [#74 — Harden G0 production-readiness boundaries](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/74)
- Prerequisite pull request:
  [#75 — Add missing pull request labeler configuration](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/75)
