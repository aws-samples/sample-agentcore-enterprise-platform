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
  52 passed.
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

## Commits and pull request

- `3ef164d` — `Harden G0 production-readiness boundaries`
- Pull request: [#74 — Harden G0 production-readiness boundaries](https://github.com/aws-samples/sample-agentcore-enterprise-platform/pull/74)
