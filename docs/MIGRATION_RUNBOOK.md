# EBA Migration Runbook

Use this runbook to rehearse moving an existing containerized agent onto
Amazon Bedrock AgentCore during an Experience-Based Acceleration (EBA).
It covers the path implemented by this repository.

| Scope | What it means |
|---|---|
| **Supported** | An arm64 image, or source built as arm64 by the accelerator, deployed to **AgentCore Runtime** in **adapter** mode |
| **Evidence-gated, customer-operated** | Existing private connectivity, retained or externally copied data, source trigger changes, traffic cutover, and rollback |
| **Not supported** | ECS-on-EC2 or amd64-only targets, native mode without the adapter, or a generic data/trigger/traffic execute command |

The adapter exposes AgentCore's port `8080`, `POST /invocations`, and
`GET /ping` contract, then forwards requests to the existing container's
configured port and paths.

## Responsibilities

| Role | Responsibility |
|---|---|
| **Platform operator** | Prepare the isolated environment, complete the manifest, build the target, run accelerator verification, and retain sanitized evidence |
| **Customer system owner** | Define and execute any data, connectivity, event-source, traffic, and rollback procedure outside the accelerator |
| **Separate approver** | Review the exact plan digest, evidence, abort thresholds, rollback, and approval window before readiness can pass |

The following are not automatically changed by this migration path:

- ECS-on-EC2 or amd64-only targets
- native mode without the adapter
- creation of VPN, Transit Gateway, private DNS, or private CA infrastructure
- customer data, webhook, scheduler, queue, or traffic-router changes

The manifest can explicitly put data copy, trigger shadowing, and canary
traffic in scope as `external-*` stages. `deploy.sh migrate readiness` then
fails closed until their named owner, separate approver, evidence reference,
rollback procedure, and timestamped approval are recorded. These are
customer-operated stages and evidence gates, not generic infrastructure
automation.

## 1. Prepare an isolated rehearsal

Do not replace the configuration of a working workshop or production
environment. Use a fresh clone or worktree and a separate project/environment:

```bash
git worktree add -b migration/customer-rehearsal \
  ../agentcore-migration-rehearsal main
cd ../agentcore-migration-rehearsal

python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export AWS_PROFILE=<rehearsal-profile>
```

Choose unique `project` and `environment` values. Confirm that the selected AWS
account and Region are approved for the rehearsal, have Bedrock model access,
and can create the resources listed in the main README prerequisites.

Keep the current source deployment running until the migration exit criteria
have passed. It is the immediate traffic rollback target.

## 2. Materialize and complete the design

Start from the migration preset:

```bash
./scripts/deploy.sh design --profile migration
```

The first validation is expected to stop on deliberate placeholders. Replace
all of them in `platform.yaml`, then run `design` again. At minimum, review:

- `project`, `environment`, `region`, and `deployment.platform_account`
- enterprise IdP tenant, client ID, and Secrets Manager **name**
- `migration.source.build.context` and `dockerfile`, or an immutable arm64
  `migration.source.image`
- the source container's `port`, `invoke_path`, and `health_path`
- plain `env` entries and secret environment-variable names under `secrets`
- `migration.target.runtime: agentcore`
- `migration.target.mode: adapter`
- which `migration.stages` are truly in scope; leave their strategy `none`
  until a customer-specific execution and rollback plan exists

Before building, run the customer preflight:

```bash
./scripts/deploy.sh doctor
```

For a source build it verifies that the context and Dockerfile exist and
confirms that CodeBuild will target `linux/arm64`. For a pre-built image it
warns until the customer independently confirms an arm64 manifest and an
immutable digest. It also checks every migration secret name without
displaying its value.

An enabled stage uses this gate shape (references only—do not paste customer
data or credentials). Add the exact digest printed for a data, trigger, or
traffic plan before setting `approved_at`:

```yaml
gate:
  owner: platform-migration-team
  approver: customer-change-approver
  evidence:
    - CHG-12345/rehearsal
    - "sha256:<exact-plan-digest>"
  rollback: CHG-12345/rollback-procedure
  approved_at: 2026-09-21T07:00:00+00:00
  expires_at: 2026-09-21T11:00:00+00:00
```

Approval must still be valid at readiness time and cannot last more than seven
days. Use the customer's shorter change window when one exists.

### Retain the existing datastore

The only built-in data adapter is `retain-source-v1`: it moves no records. The
migrated runtime keeps using a private datastore that is already governed by
the customer. Declare every retained dataset and bind it to a hostname already
listed under `migration.network.private_dependencies`:

```yaml
migration:
  network:
    private_dependencies: [database.corp.internal]
    connectivity: transit-gateway
  stages:
    data:
      strategy: retain-source
      datasets:
        - name: operational-state
          adapter: retain-source-v1
          dependency: database.corp.internal
          classification_reference: DATA-CLASS-123
          retention_reference: RETENTION-123
          identity_mapping_reference: IDENTITY-MAP-123
          validation_reference: DATA-TEST-123
      gate:
        # owner, approver, rollback, approved_at, expires_at, and evidence required
        evidence: [CHG-12345/data-review]
```

Render the canonical plan before approval:

```bash
./scripts/deploy.sh migrate data plan
```

Add the printed `sha256:…` digest to `data.gate.evidence`, then run:

```bash
./scripts/deploy.sh migrate data readiness
```

Readiness also requires the private-network gate because retaining the source
is only safe if the runtime can reach it. The digest excludes approval fields,
so approval is bound to the source/target, adapter version, and governance
references without becoming circular.

`external-copy` can record and gate a customer-operated, source-specific
procedure, but the accelerator has no generic executable copy adapter. It
never starts data movement during `design`, `build`, or `verify`. Add an
executable adapter only after defining source snapshot semantics, identity
mapping, encryption, idempotency, reconciliation, and reverse replication.

### Plan the source trigger and traffic cutover

The cutover contract is source-specific and does not create or modify the
customer's event source or router:

- HTTP can use a customer-owned percentage canary. An optional shadow rehearsal
  must be read-only or idempotent.
- Webhooks require signature-validation evidence plus a read-only or idempotent
  shadow path before a customer-owned percentage canary.
- Schedules require a dry-run or idempotent shadow target followed by an atomic
  switch; percentage canaries are rejected.
- Queues require producer-side dual-publish to a separate shadow destination,
  with idempotency evidence, followed by an atomic switch. Never add a second
  migration consumer to the source queue: it would steal production work.

For example, a webhook rehearsal and canary records the full external
procedure:

```yaml
migration:
  source:
    trigger: webhook
  stages:
    triggers:
      strategy: external-shadow
      source_reference: WEBHOOK-SOURCE-123
      shadow_reference: WEBHOOK-SHADOW-123
      safety_mode: idempotent
      idempotency_reference: IDEMPOTENCY-123
      signature_validation_reference: WEBHOOK-SIGNATURE-123
      validation_reference: WEBHOOK-TEST-123
    traffic:
      strategy: external-canary
      router_reference: ROUTER-123
      source_reference: SOURCE-ROUTE-123
      target_reference: TARGET-ROUTE-123
      metrics_reference: DASHBOARD-123
      canary_steps_percent: [5, 25, 100]
      abort:
        max_error_rate_percent: 1
        max_p95_latency_ms: 5000
        max_failed_events: 0
        observation_minutes: 15
```

Render the plans before approval:

```bash
./scripts/deploy.sh migrate cutover plan
```

Before deployment this intentionally reports a missing runtime receipt. After
the live verification in step 5, add the printed runtime digest to
`runtime.gate.evidence`, the network digest to `network.gate.evidence` when
private dependencies exist, the trigger digest to `triggers.gate.evidence`,
and the traffic digest to `traffic.gate.evidence`. Complete every applicable
gate, then run:

```bash
./scripts/deploy.sh migrate cutover readiness
```

Changing the AWS account, runtime source or effective identity/model/security
configuration, deployed artifact, VPC placement, network/DNS/CA configuration,
trigger safety, traffic step, abort threshold, or in-scope data plan changes
the relevant digest and invalidates the prior approval. Traffic approval must
follow every required prerequisite approval. The readiness command is
read-only; execution remains an approved customer operation.

Do not put secret values in `platform.yaml`. Prefer a source build because the
remote CodeBuild job produces arm64. If supplying an image, verify its arm64
manifest and pin an immutable digest before the EBA.

Validate until the design exits successfully:

```bash
./scripts/deploy.sh design
```

## 3. Ingest secrets without command-line plaintext

Each name in `migration.source.secrets` maps to:

```text
<project>/<environment>/migration/<ENV_NAME>
```

Read the value without echo and stream it to the AWS CLI. This keeps plaintext
out of the command arguments and shell history:

```bash
read -rsp "Secret value: " MIGRATION_SECRET
printf '%s' "$MIGRATION_SECRET" | aws secretsmanager create-secret \
  --name "<project>/<environment>/migration/<ENV_NAME>" \
  --secret-string file:///dev/stdin
unset MIGRATION_SECRET
```

For an existing secret, rotate it the same way:

```bash
read -rsp "New secret value: " MIGRATION_SECRET
printf '%s' "$MIGRATION_SECRET" | aws secretsmanager put-secret-value \
  --secret-id "<project>/<environment>/migration/<ENV_NAME>" \
  --secret-string file:///dev/stdin
unset MIGRATION_SECRET
```

Use the same `file:///dev/stdin` pattern for the IdP client secret and private
registry credentials, using the exact secret names declared in
`platform.yaml`. Follow the customer's approved secret-ingestion and rotation
process where it is stricter.

## 4. Close external prerequisites

Before deployment, record owners and evidence for anything outside this
repository:

- The current event source can route to AgentCore, and its payload is accepted
  by the migrated agent.
- Webhook signatures are validated; schedules have a dry-run/idempotent
  rehearsal; and queue shadows use producer dual-publish to a separate queue.
- Webhook, scheduler, or queue cutover has a tested reversal procedure.
- Required private destinations are reachable from the target VPC through an
  existing, approved VPN or Transit Gateway path.
- Private DNS resolution and private CA trust are configured and tested.
- Source and target can run in parallel without duplicate or destructive work,
  or the cutover includes an approved quiesce procedure.
- Monitoring, data handling, quotas, timeout, retry, and idempotency
  expectations are agreed with the customer.

The `migration.network`, `migration.source.trigger`, and
`migration.stages` fields document and gate these requirements; they do not
provision or cut over customer-owned systems.

## 5. Plan, build, and verify

Print the read-only migration plan and resolve every warning:

```bash
./scripts/deploy.sh migrate plan
```

Build the isolated environment:

```bash
./scripts/deploy.sh build
```

Then run configuration-aware verification:

```bash
./scripts/deploy.sh verify
```

This verifies the target, not permission to move traffic. Check the independent
cutover summary:

```bash
./scripts/deploy.sh migrate readiness
```

The command exits non-zero while traffic strategy is `none` or any enabled
stage lacks its evidence. A non-zero result does not prevent a safe
target-only rehearsal; it prevents treating that rehearsal as cutover-ready.
Use `migrate cutover plan` and `migrate cutover readiness` for the detailed
trigger/traffic procedure and approval-bound digests.

When `migration.network.private_dependencies` is non-empty, the networking
stack adds a fixed allow-list probe in the runtime's private subnets and
security group. `verify` requires DNS resolution and a hostname-verified TLS
connection on port 443 for every declared host. If
`ca_bundle_secret_name` is set, the probe reads that exact Secrets Manager
secret in memory as an additional trust bundle. It does not log addresses,
certificates, exception text, bundle contents, or customer payloads.

For a migration, verification performs a real basic invocation against the
migrated AgentCore runtime. It deliberately does not require accelerator
pattern-specific tools such as Code Interpreter, because `migration.source`
supplies the customer image. Gateway, identity, networking, observability,
alarms, guardrail, and use-case checks still run when the design includes them.
The invoke must contain decodable JSON and must not contain the adapter's
structured error status/code; an HTTP-success wrapper around a child `503`
fails verification.

AgentCore's SDK-owned `GET /ping` reports adapter-process liveness. It is not
accepted as evidence that the customer process or its dependency path is
healthy; the verified invocation performs the child health check.

Run the customer's agreed functional and non-functional tests as well. The
basic invoke proves availability of the adapter path; it does not prove the
agent's business behavior, authorization model, data correctness, capacity, or
private dependency access.

After a successful live verification, complete
`migration.stages.runtime` with the target AWS account, the runtime stack's
`RuntimeArn` and `SourceHash` outputs, the immutable ECR image digest resolved
from its `ImageUri`, and a reference to the retained verification evidence.
Complete its owner/approver gate and add the runtime digest printed by
`migrate cutover plan`. A mutable ECR tag or a successful synthesis is not a
runtime receipt.

When private dependencies are declared, also complete
`migration.network.receipt` with the deployed VPC ID, private subnet IDs,
security group IDs, the retained probe-verification reference, and the exact
tested Secrets Manager CA version when a private CA is used. Add the printed
network digest to `network.gate.evidence`. Replacing the VPC/probe or rotating
the CA requires another probe run and approval.

## 6. Cut over with an immediate rollback path

Only cut over after `deploy.sh migrate cutover readiness` returns zero and the
customer accepts the referenced evidence, abort thresholds, and rollback
trigger. The command grants no permission and changes no external system.
Record the source endpoint/configuration before changing traffic.

If a cutover check fails:

1. Stop or pause new work at the target where duplicate processing is unsafe.
2. Route the external webhook, scheduler, queue consumer, or client back to the
   recorded source.
3. Confirm source health and process a known-safe request.
4. Preserve target logs, invocation output, CloudFormation events, and the
   deployed image digest for investigation.
5. Correct the migration design and repeat plan, build, and verify in the
   isolated environment.

For an application-only regression, restore the reviewed last-known-good image
digest or source revision in `platform.yaml`, then run:

```bash
./scripts/deploy.sh design
./scripts/deploy.sh build
./scripts/deploy.sh verify
```

There is no automated rollback of external data, event-source, traffic-router,
or private-network changes. Do not destroy the source during the EBA. After
traffic has been restored or the rehearsal is accepted and evidence retained,
remove the isolated accelerator environment with:

```bash
./scripts/deploy.sh destroy
```

Review retained resources and external integrations separately; `destroy`
applies only to resources managed by this accelerator.
For non-production full-footprint cleanup, the post-destroy sweep also lists
service-created CodeBuild and Lambda log groups under the exact project and
environment prefix. Confirm their deletion, or use `--yes` when that cleanup
was pre-approved. Production mode never sweeps retained service log groups.

## EBA evidence checklist

- [ ] Approved AWS account, Region, project/environment, owners, and change window
- [ ] Completed `platform.yaml` with no placeholders or secret values
- [ ] Source revision and immutable arm64 image digest recorded
- [ ] `migrate plan` output reviewed, with every warning resolved
- [ ] Secret names, ownership, rotation, and access policies reviewed
- [ ] Build and CloudFormation results retained
- [ ] `deploy.sh verify` output retained, including the successful live invoke
- [ ] Runtime account, ARN, SourceHash, immutable ECR digest, and plan digest approved
- [ ] VPC/subnet/security-group/probe receipt and tested CA version approved
- [ ] Customer business-flow tests and expected outputs retained
- [ ] Identity, authorization, data-boundary, and private dependency tests retained
- [ ] Logs, traces, alarms, dashboards, and support ownership demonstrated
- [ ] Capacity, timeout, retry, idempotency, and failure tests completed
- [ ] Event/private-connectivity prerequisites and cutover owner confirmed
- [ ] Trigger plan digest recorded and approved when trigger work is in scope
- [ ] Traffic plan digest, steps/switch, metrics, and abort thresholds approved
- [ ] All gate expirations cover the cutover window and remain valid
- [ ] `deploy.sh migrate readiness` passed for the completed customer manifest
- [ ] Rollback trigger, source endpoint, reversal steps, and decision owner recorded
- [ ] Rollback rehearsal completed and timed
- [ ] Customer acceptance, exceptions, and follow-up actions recorded
- [ ] Cleanup or promotion decision recorded, including retained resources
