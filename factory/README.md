# Software factory

Managed agents for this repository on AgentCore Runtime. Step 1 ships the CI fixer, in dry-run mode.

| Path | What |
|---|---|
| `agent-code/factory/` | Runtime entrypoint (`server.py`: `GET /ping`, `POST /invocations`) and the CI-fixer agent (`ci_fixer.py`) |
| `Dockerfile` | ARM64 image: Python 3.13, git, Node 22 + aws-cdk CLI, ruff, the repo's `requirements.txt` |
| `infra/` | Separate CDK app: stacks `SoftwareFactoryData` and `SoftwareFactory` (the repo's `app.py` is untouched) |
| `golden/` | Recorded failing runs used to test the CI fixer |

Flow: GitHub `workflow_run` webhook -> API Gateway `POST /github` -> ingress Lambda (HMAC `X-Hub-Signature-256`,
dedupe on `X-GitHub-Delivery`, repo filter) -> SQS FIFO -> dispatcher Lambda -> runtime `software_factory`.
Each run writes one row to DynamoDB `software-factory-runs` and metrics to CloudWatch namespace `SoftwareFactory`.

While the `software-factory/github-app` secret is `{}`, the agent fixes and verifies locally, then stores the diff
and the would-be PR in the run row instead of pushing. Without App credentials it cannot download job logs, so it
re-runs the failing workflow check in its own container to get the failure.

```bash
cd factory/infra
cdk diff --all -c coordinator_role=<IAM role allowed to assume software-factory-readonly>
cdk deploy --all -c coordinator_role=<same role>
python -m pytest tests -q          # ingress HMAC gate
cd ../agent-code && python -m pytest tests -q   # golden-case classification
```
