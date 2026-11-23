# Software factory

Managed agents for this repository on AgentCore Runtime. Step 1 ships the CI fixer, in dry-run mode.

| Path | What |
|---|---|
| `agent-code/factory/` | Runtime entrypoint (`server.py`: `GET /ping`, `POST /invocations`) and the CI-fixer agent (`ci_fixer.py`) |
| `Dockerfile` | ARM64 image: Python 3.13, git, Node 22 + aws-cdk CLI, ruff, the repo's `requirements.txt` |
| `infra/` | Separate CDK app: stacks `SoftwareFactoryData`, `SoftwareFactory` and `SoftwareFactoryCicd` (the CloudFormation execution role) |
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

Deploy context, kept out of the repo: `-c asana_project=<gid> -c asana_users=<gid>[,<gid>]`.
`-c factory_paused=true` (set in `cdk.json`) keeps the queue trigger and every schedule off.

## Deploying

Deploys run from an operator workstation only. GitHub holds no AWS credentials: the
`Factory CDK` workflow synthesises the app and runs the tests, with no AWS access.

```bash
cd factory/infra
./preflight.sh -c coordinator_role=<role> -c asana_project=<gid> -c asana_users=<gid>
cdk deploy SoftwareFactoryData SoftwareFactory --app cdk.out \
  --role-arn arn:aws:iam::<account>:role/software-factory-cdk-exec
```

`software-factory-cdk-exec` is the role CloudFormation acts as. Only CloudFormation in this
account can assume it, and every resource it may touch is named for the factory.
`SoftwareFactoryCicd` holds that role; deploy it with your own credentials.

`preflight.sh` exists because `aws-cdk-lib` and the `cdk` CLI version independently. When the
library writes a newer cloud-assembly schema than the CLI can read, every command dies with
`Cloud assembly schema version mismatch`. Run it first and the failure names itself.
