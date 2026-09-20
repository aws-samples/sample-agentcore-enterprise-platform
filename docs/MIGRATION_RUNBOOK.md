# EBA Migration Runbook

Use this runbook to rehearse moving an existing containerized agent onto
Amazon Bedrock AgentCore during an Experience-Based Acceleration (EBA).
It covers the path implemented by this repository:

> **Supported path:** an arm64 image, or source built as arm64 by the
> accelerator, deployed to **AgentCore Runtime** in **adapter** mode.

The adapter exposes AgentCore's port `8080`, `POST /invocations`, and
`GET /ping` contract, then forwards requests to the existing container's
configured port and paths.

The following are not delivered by this migration path:

- ECS-on-EC2 or amd64-only targets
- native mode without the adapter
- creation of VPN, Transit Gateway, private DNS, or private CA integration
- webhook, scheduler, or queue cutover

Treat private connectivity and event-source cutover as external prerequisites
with named owners, tested procedures, and independent rollback plans.

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
- Webhook, scheduler, or queue cutover has a tested reversal procedure.
- Required private destinations are reachable from the target VPC through an
  existing, approved VPN or Transit Gateway path.
- Private DNS resolution and private CA trust are configured and tested.
- Source and target can run in parallel without duplicate or destructive work,
  or the cutover includes an approved quiesce procedure.
- Monitoring, data handling, quotas, timeout, retry, and idempotency
  expectations are agreed with the customer.

The `migration.network` and `migration.source.trigger` fields document these
requirements in the plan; they do not provision or cut over those systems.

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

## 6. Cut over with an immediate rollback path

Only cut over after the customer accepts the evidence and rollback trigger.
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

There is no automated rollback of external event sources or private-network
changes. Do not destroy the source during the EBA. After traffic has been
restored or the rehearsal is accepted and evidence retained, remove the
isolated accelerator environment with:

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
- [ ] Customer business-flow tests and expected outputs retained
- [ ] Identity, authorization, data-boundary, and private dependency tests retained
- [ ] Logs, traces, alarms, dashboards, and support ownership demonstrated
- [ ] Capacity, timeout, retry, idempotency, and failure tests completed
- [ ] Event/private-connectivity prerequisites and cutover owner confirmed
- [ ] Rollback trigger, source endpoint, reversal steps, and decision owner recorded
- [ ] Rollback rehearsal completed and timed
- [ ] Customer acceptance, exceptions, and follow-up actions recorded
- [ ] Cleanup or promotion decision recorded, including retained resources
