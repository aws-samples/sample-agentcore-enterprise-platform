# Software factory

Managed agents for this repository on AgentCore Runtime. Step 1 ships the CI fixer, in dry-run mode.

| Path | What |
|---|---|
| `agent-code/factory/` | Runtime entrypoint (`server.py`: `GET /ping`, `POST /invocations`) and the CI-fixer agent (`ci_fixer.py`) |
| `Dockerfile` | ARM64 image: Python 3.13, git, Node 22 + aws-cdk CLI, ruff, the repo's `requirements.txt` |
| `infra/` | Separate CDK app: stacks `SoftwareFactoryData`, `SoftwareFactory` and `SoftwareFactoryCicd` (the repo's `app.py` is untouched) |
| `infra/preflight.sh` | Refuses a diff or a deploy when the CDK CLI cannot read the assembly `aws-cdk-lib` writes |
| `golden/` | Recorded failing runs used to test the CI fixer |

Flow: GitHub `workflow_run` webhook -> API Gateway `POST /github` -> ingress Lambda (HMAC `X-Hub-Signature-256`,
dedupe on `X-GitHub-Delivery`, repo filter) -> SQS FIFO -> dispatcher Lambda -> runtime `software_factory`.
Each run writes one row to DynamoDB `software-factory-runs` and metrics to CloudWatch namespace `SoftwareFactory`.

While the `software-factory/github-app` secret is `{}`, the agent fixes and verifies locally, then stores the diff
and the would-be PR in the run row instead of pushing. Without App credentials it cannot download job logs, so it
re-runs the failing workflow check in its own container to get the failure.

## Asana mode (`-c factory_tracker=asana`)

The private Asana board replaces GitHub Issues as the control surface: specs and backlog stay in
Asana, and only pull requests are public. A person moves a task into **Needs design** (the
designer writes a spec), reads it in **Design review**, and approves it by moving it to **Ready for
agent** (the developer builds it). Teammates' PRs that name a task get the reviewer's summary on
it, and merging moves it to **Done**. AWS holds no Asana credential: a sync on the maintainer's
workstation carries changes both ways through an Asana MCP server. See
[`sync/README.md`](sync/README.md) for the flow, the rules and the install.

Deploy context, kept out of the repo: `-c asana_project=<gid> -c asana_users=<gid>[,<gid>]`
(GitHub variables `FACTORY_ASANA_PROJECT` and `FACTORY_ASANA_USERS` for the pipeline).
`-c factory_paused=true` (set in `cdk.json`) keeps the queue trigger and every schedule off.

## Deploying from a workstation (still supported, and the fallback)

```bash
cd factory/infra
./preflight.sh -c coordinator_role=<IAM role allowed to assume software-factory-readonly>
cdk diff --all -c coordinator_role=<same role>
cdk deploy --all -c coordinator_role=<same role>
python -m pytest tests -q          # ingress HMAC gate + the CI/CD trust assertions
cd ../agent-code && python -m pytest tests -q   # golden-case classification
```

`preflight.sh` exists because `aws-cdk-lib` and the `cdk` CLI version independently. When the
library writes a newer cloud-assembly schema than the CLI can read, every command dies with
`Cloud assembly schema version mismatch`, which reads like a broken app. Run it first and the
failure names itself: `CDK_MANIFEST_SCHEMA_MISMATCH`, with both versions and the fix.

## Deploying from GitHub

`.github/workflows/factory-cdk.yml`. Two AWS roles, both reached by OIDC with no stored key:

| Role | Who gets it | What it can do |
|---|---|---|
| `software-factory-gha-diff` | any pull request, and pushes to `factory/main-v2` | read the two stacks' templates. Nothing else, nowhere in the account. |
| `software-factory-gha-deploy` | only a job running on the `deploy` environment | start a CloudFormation change set on `SoftwareFactory` and `SoftwareFactoryData`, upload CDK assets, and hand `software-factory-cdk-exec` to CloudFormation. It can change no resource directly. |

`software-factory-cdk-exec` is the role CloudFormation itself acts as. Only
`cloudformation.amazonaws.com` can assume it, so its permissions are reachable only by changing
one of the two stacks the deploy role may name.

Neither role can assume the CDK bootstrap roles: `cdk-hnb659fds-cfn-exec-role-*` carries
`AdministratorAccess`, which would hand GitHub the whole account. The stacks therefore use
`CliCredentialsStackSynthesizer` (no role assumption) and the deploy step passes
`--role-arn software-factory-cdk-exec`.

`SoftwareFactoryCicd` holds the trust and the roles, and is **not** in the set the deploy role
can reach, so the pipeline cannot widen its own grant. Deploy that stack from a workstation.

One-time setup in GitHub: an environment named `deploy` with the maintainer as a required
reviewer. Create it **before** the first deploy run -- GitHub creates a missing environment on
first use with no protection rules at all. No repository secret is needed; OIDC replaces it.
