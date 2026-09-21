# Enterprise Agentic AI Platform Accelerator

Deploy a secure, governed foundation for production AI agents on Amazon Bedrock AgentCore. This **open-source, modular** project works as a self-service starter kit, a foundation to tailor to your environment, or a guided team build.

**📖 Documentation site:** [aws-samples.github.io/sample-agentcore-enterprise-platform](https://aws-samples.github.io/sample-agentcore-enterprise-platform/) — start with [How it works](https://aws-samples.github.io/sample-agentcore-enterprise-platform/#/how-it-works).

> **Production readiness:** This repository is a platform baseline that must be
> tailored and approved for each customer's security, reliability, data, and
> compliance requirements. The
> [`Production Readiness Plan`](docs/PRODUCTION_READINESS_PLAN.md) defines the
> work and release evidence required before describing a deployment as
> production-ready.

Current release: **v0.1.0**. Review the
[`support matrix`](docs/SUPPORT_MATRIX.md),
[`known limitations`](docs/KNOWN_LIMITATIONS.md), and
[`release notes`](docs/releases/v0.1.0.md) before using it with a customer.

## What You Get

- **An AI platform for production agents:** AgentCore Runtime, Gateway, Identity, Memory, and observability.
- **Your choice of agent framework:** use Strands Agents, LangGraph, Claude Agent SDK, without rebuilding the infrastructure.
- **Security controls as you need them:** turn on VPC isolation, KMS encryption, CloudTrail, SCPs, Cedar policies, Bedrock Guardrails, resource policies, and traceability alerting.
- **A repeatable way to ship:** deploy by profile, team, module, or CI/CD. Then verify the result with the included scripts and local dashboard.

> **Security details:** See [`docs/SECURITY_CONTROLS.md`](docs/SECURITY_CONTROLS.md) for available controls and enablement guidance.

## How to use this accelerator

Three phases, three commands: **Design → Build → Verify**.

| Phase | What you do | Command | Where to start |
|-------|-------------|---------|----------------|
| **Design** | Pick a profile or write your own `platform.yaml` (accounts, IdP, framework, controls); scaffold your use case into it; validate and see the exact stacks it produces. Nothing is deployed. | `./scripts/deploy.sh design --profile <name>`, `./scripts/deploy.sh usecase new <name>` | [Design](#design), [`docs/PLATFORM_YAML.md`](docs/PLATFORM_YAML.md) |
| **Build** | Stand up the platform and every use case in the design with one command. | `./scripts/deploy.sh build` | [Build](#build) |
| **Verify** | Re-test every promise the design made, platform and use cases alike; exits non-zero on any failure. | `./scripts/deploy.sh verify` | [Verify](#verify), [Dashboard](#dashboard) |

Before you build, run `./scripts/deploy.sh doctor`. It checks your local tools,
effective account and Region, required secret references, Bedrock model
metadata, and migration image inputs without changing AWS resources.

> **Doing this as a workshop?** [`docs/PARTICIPANT_GUIDE.md`](docs/PARTICIPANT_GUIDE.md)
> walks the modules in order with expected timings and what proves each one worked.
> When something breaks, [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) is
> organised by symptom.
> Migrating an existing container during an EBA? Use the
> [`EBA Migration Runbook`](docs/MIGRATION_RUNBOOK.md) for the supported path,
> secret handling, cutover, rollback, and evidence checklist.


## Choose Your Starting Point

Pick the profile that looks most like your job today. It is a starting point, you can further customize your deployment later.

| Profile | Good fit when you are... | Scope (guided modules) |
|---------|--------------------------|------------------------|
| `greenfield` | Building new agents from scratch | Identity, gateway, one agent runtime, and observability |
| `migration` | Rehosting a compatible container or source build as an arm64 AgentCore Runtime behind the supplied adapter | Identity, runtime migration, gateway integration, and observability |
| `multi-agent` | Building specialist agents that work together | Gateway, orchestrator, A2A runtimes, and observability |
| `platform-team` | Setting up shared infrastructure for your organization | Full platform, including memory, A2A, networking, and security |
| `security-focused` | Starting with compliance and hardening | One-agent platform, networking, security, policy, egress, and traceability controls |
| `production` | Preparing a long-lived customer baseline | Enterprise IdP, model allow-list, enforced Cedar and guardrails, networking, audit, alarms, encryption, and retained state |

A profile defines a starting footprint: `design --profile <name>` writes the
profile's preset ([`presets/`](presets/)) to `platform.yaml` and prints the
stacks it produces, then `build` deploys the validated design. The disposable
profiles also work as guided lesson plans through `workshop --profile <name>`.
The `production` profile is intentionally design/build only and refuses the
guided workshop runner. Multi-account topologies have their own presets,
`federated` and `distributed`; see
[`docs/MULTI_ACCOUNT.md`](docs/MULTI_ACCOUNT.md).

The production preset is a secure template, not a launch approval. Replace all
sentinels, complete the DRAFT governance artifacts under
[`docs/`](docs/PRODUCTION_READINESS_PLAN.md), and pass the release gates for the
specific customer. Production mode fails validation if enterprise identity,
networking, audit, resource policies, egress controls, guardrails, enforced
Cedar authorization, model allow-listing, tracing, and monitored alarms are
not configured. Its stateful resources survive stack removal; their final
disposition must follow the customer's approved retention and deletion plan.


## Getting Started

### Prerequisites

Before you deploy:

- **AWS credentials:** permission to create IAM, Cognito, ECR, CodeBuild, Amazon Bedrock and Bedrock AgentCore resources. The deploy script validates them before making changes.
- **Bedrock model access:** enable access to the model your agents use (default: Anthropic Claude) in the Amazon Bedrock console, in the Region you deploy to. Deployment succeeds without it, but every agent invocation fails at runtime.
- **Local tooling:** Python 3.13 (as `python3.13`), a CDK-supported Node.js LTS release (20, 22, or 24) with npm, AWS CLI v2, and bash 4+ (macOS ships 3.2 — `brew install bash`). The script checks these and installs the AWS CDK CLI if it is missing. A container runtime is **not** required: agent images are built remotely in AWS CodeBuild, and it is only useful for testing an image locally.
- **Region:** pick a Region where AgentCore and your chosen Bedrock model are available. The default is `us-east-1`.
- **Cost awareness:** networking profiles create a NAT gateway and VPC endpoints with hourly billing. Enabling Transaction Search changes account-level span pricing. Tear down resources when you finish testing.

After creating and completing `platform.yaml`, run the read-only preflight:

```bash
./scripts/deploy.sh doctor
```

Every `[FAIL]` is a build blocker. `[WARN]` identifies a decision or check that
still needs human validation, such as the architecture of a pre-built
migration image. A green model metadata check proves that the configured model
or inference profile can be discovered; `deploy.sh verify` proves actual
inference access after deployment.

### Architecture

Want the full picture? Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the Mermaid diagram and request flows that the repository verifies end to end.

![Enterprise Agentic AI Platform, accounts and trust boundaries](docs/architecture-accounts.png)

Applications sign in once and carry a validated JWT. Agents run in workload accounts, one per use case, owned by the teams that build them. The platform account holds the shared services, and cross-account trust is OAuth token exchange, with no cross-account IAM on the data plane. Governance and security tooling sit in their own accounts and apply to all of them.

![Enterprise Agentic AI Platform architecture](docs/architecture.png)

The Mermaid diagram in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the source of truth for the request flows, and it renders directly on GitHub.

### Design

Pick a profile and let `design` write it to `platform.yaml`, then edit the file until the plan it prints is the platform you want. Nothing is deployed in this phase; the only AWS call reads your account id.

```bash
# Install dependencies once
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Pick a profile: writes platform.yaml, validates it, prints the stacks it produces
./scripts/deploy.sh design --profile greenfield
# Other profiles: migration | multi-agent | platform-team | security-focused | production

# Edit platform.yaml (accounts, IdP and identity.mode, framework, controls...)
# and re-run `design` to re-validate. Every key: docs/PLATFORM_YAML.md
./scripts/deploy.sh design

# Read-only check of tools, AWS target, secrets, model, and migration inputs
./scripts/deploy.sh doctor

# Add your use case to the design (scaffolds use-cases/<name>/ and enables it)
./scripts/deploy.sh usecase new my-agent
```

### Build

One command stands up the platform and every use case in the design. You can narrow the build by team or module, or take the guided run.

```bash
# Build the platform and the use cases in the design
./scripts/deploy.sh build

# Build a smaller scope instead
./scripts/deploy.sh build --team agent  # Agent team stacks only
./scripts/deploy.sh build --module 4    # Identity integration

# Build a design in CI/CD
NON_INTERACTIVE=1 AWS_REGION=us-east-1 ./scripts/deploy.sh build

# Use the guided run for explanations and checks after each module
# The script needs bash 4 or newer. macOS includes bash 3.2.
# Install a newer version with `brew install bash`, then run `bash scripts/deploy.sh ...`.
./scripts/deploy.sh workshop                        # Default profile: greenfield
./scripts/deploy.sh workshop --profile multi-agent  # Or migration|platform-team|security-focused
./scripts/deploy.sh workshop --from 6               # Resume at module 6
./scripts/deploy.sh workshop --dry-run              # Show the plan without AWS calls
```


### Verify

`verify` re-tests every promise the design made — platform and use cases alike — and exits non-zero on any failure. The other tools below isolate a single component when something is off.

```bash
export AWS_PROFILE=<your-profile>   # Skip if you use default credentials
# AWS_REGION is optional: the tools read the region you deployed with
# (platform.yaml or workshop.env). Set it only to override.

# Health check: runs every check your configuration promises (gateway, memory,
# observability, live agent invokes, ...) and exits non-zero on any failure.
./scripts/deploy.sh verify

# Invoke the deployed agent. Add --session <id> to continue a conversation.
# (Ask the default orchestrator about its role, not its tools — it delegates
# to sub-agents and deliberately has none of its own.)
python scripts/invoke.py "Hello! What kinds of tasks can you help with?"

# Use the AG-UI protocol for agui-* patterns.
python scripts/invoke.py --agui "Hello! What kinds of tasks can you help with?"

# List the gateway's MCP tools.
python scripts/invoke.py --tools

# Call the gateway directly: MCP tools/list and one tools/call.
python scripts/test_gateway.py
```

For the wider test plan, read [`docs/TESTING.md`](docs/TESTING.md). For live resource status, use the [Dashboard](#dashboard).

## Dashboard

Want to see the platform come together? The local dashboard reads the same deployment
contract as `deploy.sh` and shows three views: an **Overview** (stacks this profile promises,
grouped by layer, with outputs and resources one click away), a live **Architecture** map, and
the published **Parameters** that make up the platform interface. It runs on your machine,
polls your AWS account, and is plain HTML served by Python: no build step, no dependencies.

Run both commands from the repository root. The dashboard is only available on localhost.

```bash
# Terminal 1: poller. Writes dashboard/public/status.json every 15 seconds.
# Polls the region you deployed with; set AWS_REGION only to override.
AWS_PROFILE=<your-profile> .venv/bin/python dashboard/monitor.py

# Terminal 2: loopback-only web server. Open http://127.0.0.1:8888.
python3 -m http.server 8888 --bind 127.0.0.1 -d dashboard/public
```

![AgentCore deployment dashboard monitor tab](docs/dashboard-monitor.png)

## Clean Up

Destroy resources when you no longer need them

```bash
./scripts/deploy.sh destroy

# Or destroy one stack.
./scripts/deploy.sh destroy --stack <stack-name>
```

For networking deployments, AgentCore network interfaces can outlive a runtime for up to eight hours. A cleanup may need a retry. See [Network isolation](#run-runtimes-in-your-vpc-enable_networking) for the detail.


## Understand the AWS CloudFormation Stacks

### Stacks

Profiles select from these stack building blocks.

| Stack | Resources | What it does |
|-------|-----------|--------------|
| `auth` | Cognito User Pool, 3 clients, SSM params — or, with `identity.mode: direct`, only the SSM params describing your Entra ID issuer | Sets up identity: Cognito with optional federated IdP, or your IdP as the issuer |
| `identity` | OAuth2 credential providers | Supports 3LO delegation for Google, GitHub, and Notion |
| `memory` | CfnMemory + strategies | Adds semantic and user-preference memory |
| `gateway` | CfnGateway + Lambda targets | Exposes MCP tools with CUSTOM_JWT auth |
| `runtime-orchestrator` | ECR, CodeBuild, CfnRuntime | Runs the main HTTP agent |
| `runtime-code-agent` | ECR, CodeBuild, CfnRuntime | Runs an A2A sub-agent for code tasks |
| `runtime-research-agent` | ECR, CodeBuild, CfnRuntime | Runs an A2A sub-agent for research |
| `observability` | Vended logs, X-Ray delivery | Collects monitoring data for each resource |
| `networking` *(optional)* | VPC, private subnets, endpoints, runtime SG | Puts agents in your VPC ([details](#run-runtimes-in-your-vpc-enable_networking)) |
| `security` *(optional)* | KMS CMK, CloudTrail | Adds security hardening |

## Customize, Operate, and Extend

Use these sections when you need to change how the platform is deployed, secured, monitored, or integrated. The main entry points:

| You want to... | Start here |
|----------------|------------|
| Configure the platform declaratively | [`platform.yaml`](#customize-a-deployment), starting from a preset in [`presets/`](presets/) |
| Use your corporate IdP (Entra ID, Okta, Ping) — federated through Cognito, or Entra ID as the issuer with no Cognito | [`docs/ENTERPRISE_IDP.md`](docs/ENTERPRISE_IDP.md) |
| Deploy across multiple accounts (federated) | [`docs/MULTI_ACCOUNT.md`](docs/MULTI_ACCOUNT.md) |
| Add your own tools to the gateway | [`docs/GATEWAY_TARGETS.md`](docs/GATEWAY_TARGETS.md) |
| Build a use case on top of the platform | `./scripts/deploy.sh usecase new <name>`, then [`CONTRIBUTING_USE_CASES.md`](CONTRIBUTING_USE_CASES.md) with [`docs/PLATFORM_INTERFACE.md`](docs/PLATFORM_INTERFACE.md) |
| Rehearse an existing-agent migration in an EBA | [`docs/MIGRATION_RUNBOOK.md`](docs/MIGRATION_RUNBOOK.md) |

### Choose an Agent Framework
Each runtime stack builds one agent from the `agent-code/` directory. Pick the framework you want here; the CDK infrastructure does not change.

Available patterns: `orchestrator` (default), `strands-agent`, `langgraph-agent`, `claude-sdk-agent`, `claude-sdk-multi-agent`, `agui-strands-agent`, and `agui-langgraph-agent`. A bad value stops the deployment before it starts.

```bash
# Pick a framework. The script saves the choice in workshop.env for later runs.
AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6
```

`./scripts/deploy.sh deploy` asks for the pattern in an interactive run. The guided command prints the active pattern before its first module.
Use `deploy.sh` for every update to an existing environment: it owns
consumer-first identity migrations and credential-rotation checkpoints. Direct
`cdk deploy` is supported only for disposable test stacks and synth/diff work.

The agent applications and shared utilities build on patterns from [fullstack-solution-template-for-agentcore](https://github.com/aws-samples/fullstack-solution-template-for-agentcore) (FAST). The CDK stacks are specific to this accelerator.

### Track Costs per Component

Every stack is tagged with `Project`, `Environment`, and `Component` (the stack's suffix in the deployment contract — `gateway`, `memory`, `runtime-orchestrator`, use-case stacks included). To see them in Cost Explorer, activate the tags once per payer account — takes effect within about 24 hours:

```bash
aws ce update-cost-allocation-tags-status --cost-allocation-tags-status \
  Status=Active,TagKey=Project Status=Active,TagKey=Environment Status=Active,TagKey=Component
```

Then group by the `Component` tag in Cost Explorer to split spend per stack. Scope: tags attribute *resource* costs (NAT, endpoints, CloudWatch, CodeBuild). Two gaps to know about: a few resource types (AgentCore Memory, SSM parameters) do not accept CloudFormation tags, and Bedrock model inference — usually the largest line — is not covered by resource tags at all; attributing inference requires Application Inference Profiles, which is on the roadmap.

### Run Runtimes in Your VPC (`enable_networking`)

Use this when your agents need private access to resources in your VPC or tighter outbound network controls. Set `enable_networking=true` to run runtimes in your VPC. AgentCore creates network interfaces in private subnets and attaches them to a security group with HTTPS-only egress and no inbound access. Traffic goes out through the NAT gateway. Interface endpoints cover Bedrock, ECR, CloudWatch Logs, and AgentCore Gateway. ECR layer pulls use the free S3 gateway endpoint.

This is not an air-gapped VPC.

- **What you get:** no public network path from the agent, private access to resources in your VPC, and security-group control over destinations.
- **What you do not get:** the private subnets still have a NAT route because agents call AWS APIs and some patterns use the public internet. Remove the NAT only after adding endpoints for every service your agents call.

Do not trust the flag alone. After deploying, confirm the runtimes actually landed in private subnets:

```bash
python scripts/check_network.py                  # Supported subnets and runtimes in the VPC
python scripts/check_network.py --expect-public   # Deployments without networking
```

> **Availability Zones:** AgentCore supports VPC connectivity in selected AZs. An AZ name such as `us-east-1a` maps to a different AZ ID, such as `use1-az1`, in each account. A VPC that works in one account can still fail when another account creates a runtime. `check_network.py` catches that during the networking module. The guided command runs it as module C's verification.

> **Teardown:** network interfaces can remain for up to 8 hours after a runtime stops using VPC mode. During that window, deleting the networking stack can fail because the private subnets and runtime security group still have dependencies. The NAT gateway and VPC endpoints, which create the hourly charges, are deleted in the same run. Try the destroy again after the interfaces age out: `aws ec2 describe-network-interfaces --filters Name=interface-type,Values=agentic_ai`.

### Customize a Deployment

Use these settings to change the platform's name, environment, identity provider, optional capabilities, model, or agent framework. Set configuration with CDK context (`-c key=value`) or environment variables.

| Context key | Environment variable | Default | Meaning |
|-------------|----------------------|---------|---------|
| `project` | `PROJECT_NAME` | `agentcore-workshop` | Project identifier |
| `deployment_mode` | `DEPLOYMENT_MODE` | `workshop` | `production` enables retained lifecycle and requires the complete secure control set in `platform.yaml` |
| `environment` | `ENVIRONMENT` | `dev` | Environment name |
| `region` | `AWS_REGION` | `us-east-1` | AWS Region |
| `idp_type` | `IDP_TYPE` | `cognito` | IdP: cognito/entra_id/okta/ping |
| `idp_mode` | `IDP_MODE` | `brokered` | `brokered`: Cognito issues tokens, the IdP signs users in. `direct`: the IdP (Entra ID) issues tokens, no Cognito |
| `enable_networking` | `ENABLE_NETWORKING` | `false` | Create the VPC stack |
| `enable_security` | `ENABLE_SECURITY` | `false` | Create the security stack |
| `require_guardrails` | `REQUIRE_GUARDRAILS` | `false` | Deny Bedrock inference without a Guardrail on the runtime roles and inject a baseline guardrail into every agent (not supported by the claude-sdk patterns) |
| `enable_a2a` | `ENABLE_A2A` | `true` | Create A2A agent stacks |
| `model_id` | `MODEL_ID` | *(in code for each pattern)* | Override the Bedrock model for all agents, for example `us.anthropic.claude-sonnet-5` |
| `agents.allowed_models` | `ALLOWED_MODELS` | *(unrestricted)* | Optional model allow-list. When set, `model_id` must be one of these and the runtime role's Bedrock permissions are scoped to exactly these models |
| `agent_pattern` | `AGENT_PATTERN` | `orchestrator` | Pattern built for the runtime. See [Agent Pattern Selection](#choose-an-agent-framework). |
| `enable_transaction_search` | `ENABLE_TRANSACTION_SEARCH` | `true` | Configure CloudWatch Transaction Search. This setting is account scoped. See [details](#search-agent-traces). |
| `enable_alarms` | `ENABLE_ALARMS` | `false` | Create CloudWatch alarms, an SNS ops topic, and the platform dashboard. See [details](#alarms-and-the-platform-dashboard). |
| `alarm_email` | `ALARM_EMAIL` | *(none)* | Email address subscribed to the alarm topic. Requires `enable_alarms`. |
| `agents.memory.event_expiry_days` | `MEMORY_EVENT_EXPIRY_DAYS` | `30` | Bounded AgentCore Memory event retention |
| `observability.log_retention_days` | `LOG_RETENTION_DAYS` | `30` | Platform log retention: 30, 90, 180, or 365 days |

Prefer a file you can review and commit? `platform.yaml` is the declarative manifest for the same settings and more (multi-account strategy, gateway tools, security controls). Deploying with `--profile <name>` writes it for you from [`presets/`](presets/), or copy a preset yourself and validate it offline:

```bash
python -m infra_utils.platform_config platform.yaml
```

Interactive `./scripts/deploy.sh deploy` answers go into the gitignored `workshop.env` file and are used on later runs. Environment variables win over `platform.yaml`, which wins over `workshop.env`, which wins over defaults. Check the current values with `./scripts/deploy.sh config`. Start over with `./scripts/deploy.sh config --reset`.

Secrets such as an IdP client secret or API keys are never written to `workshop.env`. They go to AWS Secrets Manager.

### Verify Caller Identity

Agents identify callers from the JWT in the `Authorization` header, not the request body. See [`docs/IDENTITY.md`](docs/IDENTITY.md) for how token validation works and why the agent checks it twice.

### Search Agent Traces

Runtime traces won't appear until the account is configured to receive them. See [`docs/TRACING.md`](docs/TRACING.md) for the setup and verification steps.

### Alarms and the Platform Dashboard

With `observability.alarms: true` (or `-c enable_alarms=true`) the observability stack also creates CloudWatch alarms per deployed resource (runtime and gateway errors, throttles, and p99 latency; memory event errors; account-level Bedrock throttles and server errors), an SNS topic named `{project}-{environment}-platform-alarms` that receives every alarm and OK transition, and a CloudWatch dashboard named `{project}-{environment}-platform`. `INSUFFICIENT_DATA` is the normal state for the error alarms — those metrics only emit when something fails. Set `observability.alarm_email` to subscribe an inbox; SNS sends a confirmation email that must be accepted before notifications arrive. Verify with `python scripts/check_alarms.py` (also part of `deploy.sh verify` when the flag is on).

### Extend the Platform with Other Stacks

New stacks can read values from the platform through SSM Parameter Store. Here are the paths available after deployment:

```
/{project}/{environment}/auth/issuer-url
/{project}/{environment}/auth/user-pool-id
/{project}/{environment}/auth/app-client-id
/{project}/{environment}/identity/gateway-credential-provider-name
/{project}/{environment}/gateway/url
/{project}/{environment}/memory/memory-id
/{project}/{environment}/runtimes/{component}/arn
```
