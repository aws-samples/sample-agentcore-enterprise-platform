# AgentCore Workshop CDK Platform

Modular AWS CDK (Python) infrastructure for the **AgentCore Platform and Security Accelerator** 2-day workshop.

> **Security controls:** opt-in SCPs, VPC endpoint / IAM policies, Cedar policies, resource
> policies, Bedrock Guardrails + egress interceptor, and traceability alerting are documented
> in [`docs/SECURITY_CONTROLS.md`](docs/SECURITY_CONTROLS.md). Test guide: [`docs/TESTING.md`](docs/TESTING.md).

![Workshop Dashboard — Monitor Tab](docs/dashboard-monitor.png)

## Architecture

Maintained source: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (Mermaid diagram + verified request flows).

![AgentCore Workshop Architecture](docs/architecture.png)

## Stacks

| Stack | Resources | Description |
|-------|-----------|-------------|
| `auth` | Cognito User Pool, 3 clients, SSM params | Identity foundation with federated IdP support |
| `identity` | OAuth2 credential providers | 3LO delegation (Google, GitHub, Notion) |
| `memory` | CfnMemory + strategies | Semantic + user preference memory |
| `gateway` | CfnGateway + Lambda targets | MCP gateway with CUSTOM_JWT auth |
| `runtime-orchestrator` | ECR, CodeBuild, CfnRuntime | Main agent (HTTP protocol) |
| `runtime-code-agent` | ECR, CodeBuild, CfnRuntime | A2A sub-agent for code tasks |
| `runtime-research-agent` | ECR, CodeBuild, CfnRuntime | A2A sub-agent for research |
| `observability` | Vended logs, X-Ray delivery | Per-resource monitoring |
| `networking` *(optional)* | VPC, private subnets, endpoints, runtime SG | Runs the agents inside your VPC ([details](#network-isolation-enable_networking)) |
| `security` *(optional)* | KMS CMK, CloudTrail | Security hardening |

## Quick Start

```bash
# 1. Install dependencies
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Guided workshop (recommended for participants): walks your profile's modules
#    one by one — explains what each builds and why, deploys it, verifies it live, pauses.
# Note: the deploy script requires bash 4 or newer. macOS ships bash 3.2 —
# install a current bash with `brew install bash`, then run `bash scripts/deploy.sh ...`.
./scripts/deploy.sh workshop                        # default profile: greenfield
./scripts/deploy.sh workshop --profile multi-agent  # or migration|platform-team|security-focused
./scripts/deploy.sh workshop --from 6               # resume from module 6
./scripts/deploy.sh workshop --dry-run              # preview the plan, no AWS calls

# 2b. Plain deploy (everything at once, interactive)
./scripts/deploy.sh deploy

# 3. Deploy specific workshop module
./scripts/deploy.sh deploy --module 4    # Identity Integration

# 4. Deploy for a specific team
./scripts/deploy.sh deploy --team agent  # Agent team stacks only

# 5. Deploy with a customer profile
./scripts/deploy.sh deploy --profile greenfield

# 6. Non-interactive (CI/CD)
NON_INTERACTIVE=1 AWS_REGION=us-east-1 ./scripts/deploy.sh deploy
```

## Testing the Deployment

Full architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```bash
export AWS_PROFILE=<your-profile>   # skip if using default credentials
export AWS_REGION=us-east-1

# Health check: token → invoke → resource status
python scripts/test.py

# Invoke the deployed agent (add --session <id> to continue a conversation)
python scripts/invoke.py "What tools do you have?"

# For the agui-* patterns, use the AG-UI protocol (RunAgentInput + SSE events)
python scripts/invoke.py --agui "What tools do you have?"

# List the gateway's MCP tools
python scripts/invoke.py --tools

# Gateway direct: MCP tools/list + one tools/call
python scripts/test_gateway.py
```

For live status of every deployed resource, run the [Dashboard](#dashboard).

## Workshop Module Mapping

| Module | Description | CDK Stacks |
|--------|-------------|------------|
| 3 | Infrastructure Blueprint | `auth` |
| 4 | Identity Integration | `auth`, `identity` |
| 5 | Gateway & Registry | `gateway` |
| 6 | Agent Deployment | `runtime-orchestrator` |
| 8 | Agent-to-Agent (A2A) | `runtime-code-agent`, `runtime-research-agent` |
| 9 | Observability | `observability` |
| A | Memory | `memory` |

## Agent Pattern Selection

The `agent_pattern` CDK context variable selects which `agent-code/<pattern>/` directory
the runtime stack builds and deploys — participants pick their framework without touching
infrastructure code:

Patterns: `orchestrator` (default), `strands-agent`, `langgraph-agent`, `claude-sdk-agent`,
`claude-sdk-multi-agent`, `agui-strands-agent`, `agui-langgraph-agent`. An unknown value is
rejected before any deployment starts.

```bash
# Pick a framework — saved to workshop.env and reused on later runs
AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6

# Or straight through the CDK CLI
cdk deploy agentcore-workshop-dev-runtime-orchestrator -c agent_pattern=claude-sdk-agent
```

`./scripts/deploy.sh deploy` prompts for the pattern interactively; the guided
`workshop` action prints the pattern in use before the first module.

Agent application code and shared utilities are adapted from the
[fullstack-solution-template-for-agentcore](https://github.com/aws-samples/fullstack-solution-template-for-agentcore)
(FAST) patterns; the CDK stacks are original to this workshop.

## Network isolation (`enable_networking`)

With `enable_networking=true` the runtimes are placed **in** the VPC: AgentCore
creates network interfaces in the private subnets, attached to a security group that
allows HTTPS egress only and no inbound. Egress leaves through the NAT gateway, with
interface endpoints for Bedrock, ECR (api + dkr), CloudWatch Logs and the AgentCore
Gateway, plus the free S3 gateway endpoint that ECR layer pulls use.

What this does and does not give you:

- **Does**: no public network path from the agent, private access to resources in your
  VPC, and security-group control over what the agent can reach.
- **Does not**: an air-gapped VPC. The private subnets keep a NAT route because agents
  reach AWS APIs and, for some patterns, the public internet. Remove the NAT only after
  adding endpoints for every service your agents call.

Verify placement rather than trusting the flag:

```bash
python scripts/check_network.py                  # subnets in supported AZs + runtimes in VPC
python scripts/check_network.py --expect-public   # for deployments without networking
```

> **Availability Zones**: AgentCore supports VPC connectivity in specific AZs, and an AZ
> *name* (`us-east-1a`) maps to a different AZ *id* (`use1-az1`) in every account. A VPC
> that works in one account can fail at runtime creation in another. `check_network.py`
> catches this at the networking module instead, and the guided workshop runs it as
> module C's verification.

> **Teardown**: AgentCore's network interfaces persist for up to 8 hours after a runtime
> stops using VPC mode, so destroying the networking stack in that window fails on the
> private subnets and the runtime security group with "has dependencies and cannot be
> deleted". The NAT gateway and VPC endpoints — everything that bills hourly — are deleted
> in the same run, so the leftovers are free. Re-run the destroy once the interfaces age
> out (`aws ec2 describe-network-interfaces --filters Name=interface-type,Values=agentic_ai`).

## Tracing (CloudWatch Transaction Search)

AgentCore runtimes emit OTLP spans whether or not the account is set up to receive them.
If the account's X-Ray trace segment destination is still `XRay`, every span batch is
rejected with HTTP 400 and no trace ever appears — while the deployment reports success.
The observability stack therefore configures the two prerequisites from
[Enable transaction search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html):
a CloudWatch Logs resource policy allowing X-Ray to write the span log groups, and the
trace segment destination set to `CloudWatchLogs`.

```bash
python scripts/check_observability.py   # destination + span policy + log deliveries
```

> **Account-level setting**: both are account- and Region-scoped, not per-stack. Where a
> platform team owns tracing centrally, deploy with `-c enable_transaction_search=false`.
> Destroying the stack deliberately does **not** revert the destination — other workloads
> in the account may depend on it by then.

> **Span visibility**: the prerequisites above stop the rejections. Delivering spans to the
> agent's own log group (instead of the shared `aws/spans`) additionally needs
> `aws-opentelemetry-distro>=0.18.0` in the agent image; the runtime currently bundles
> `0.16.0`, which ignores span destination configuration. Enabling Transaction Search also
> switches the account to CloudWatch span ingestion pricing, with 1% indexed for free.

## Customer Profiles

| Profile | Networking | Security | A2A |
|---------|-----------|----------|-----|
| `greenfield` | ✗ | ✗ | ✗ |
| `migration` | ✗ | ✗ | ✗ |
| `multi-agent` | ✗ | ✗ | ✓ |
| `platform-team` | ✓ | ✓ | ✓ |
| `security-focused` | ✓ | ✓ | ✗ |

## Team Workstreams

| Team | Stacks |
|------|--------|
| `platform` | networking, auth, identity, gateway, observability |
| `agent` | runtime-*, memory |
| `security` | security, observability |

## Dashboard

Live workshop monitoring dashboard with approach explainer and deployment status:

Run both from the repo root; the dashboard is served on localhost only.

```bash
# Terminal 1 — poller (writes dashboard/public/status.json every 15s)
AWS_PROFILE=<your-profile> AWS_REGION=us-east-1 .venv/bin/python dashboard/monitor.py

# Terminal 2 — server (dashboard at http://localhost:8888)
python3 -m http.server 8888 -d dashboard/public
```

## Configuration

All configuration via CDK context (`-c key=value`) or environment variables:

| Context Key | Env Var | Default | Description |
|-------------|---------|---------|-------------|
| `project` | `PROJECT_NAME` | `agentcore-workshop` | Project identifier |
| `environment` | `ENVIRONMENT` | `dev` | Environment name |
| `region` | `CDK_DEFAULT_REGION` | `us-east-1` | AWS region |
| `idp_type` | `IDP_TYPE` | `cognito` | IdP: cognito/entra_id/okta/ping |
| `enable_networking` | `ENABLE_NETWORKING` | `false` | Enable VPC stack |
| `enable_security` | `ENABLE_SECURITY` | `false` | Enable security stack |
| `enable_a2a` | `ENABLE_A2A` | `true` | Enable A2A agent stacks |
| `model_id` | `MODEL_ID` | *(in-code per pattern)* | Bedrock model ID override for all agents (e.g. `us.anthropic.claude-sonnet-5`) |
| `agent_pattern` | `AGENT_PATTERN` | `orchestrator` | Agent framework pattern built for the runtime (see [Agent Pattern Selection](#agent-pattern-selection)) |
| `enable_transaction_search` | `ENABLE_TRANSACTION_SEARCH` | `true` | Configure CloudWatch Transaction Search — account-level ([details](#tracing-cloudwatch-transaction-search)) |

Interactive answers from `./scripts/deploy.sh deploy` are saved to `workshop.env` (gitignored)
and reused on later runs. Precedence: environment variable > saved `workshop.env` > default.
Inspect with `./scripts/deploy.sh config`; start fresh with `./scripts/deploy.sh config --reset`.
Secrets (IdP client secret, API keys) are never persisted — they go to AWS Secrets Manager.

## Cross-Stack Communication

All stacks publish key outputs to SSM Parameter Store:

```
/{project}/{environment}/auth/issuer-url
/{project}/{environment}/auth/user-pool-id
/{project}/{environment}/auth/app-client-id
/{project}/{environment}/identity/gateway-credential-provider-name
/{project}/{environment}/gateway/url
/{project}/{environment}/memory/memory-id
/{project}/{environment}/runtimes/{component}/arn
```
