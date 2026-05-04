# FAST Integration Tracker

**Tracking integration of components from [fullstack-solution-template-for-agentcore](https://github.com/aws-samples/fullstack-solution-template-for-agentcore) into the AgentCore Workshop CDK platform.**

*Last updated: 2026-05-03*

---

## Reference

| | Path |
|---|---|
| **FAST repo** | `/Users/robcata/Local/Projects/AgentCore/fullstack-solution-template-for-agentcore` |
| **Workshop repo** | `/Users/robcata/Local/Projects/AgentCore/AIAccelerator/IaC/workshop-cdk` |

---

## Activity Status

Status legend: 🔴 Not Started · 🟡 In Progress · 🟢 Done · ⏭️ Skipped

### Tier 1 — Direct Reuse

| # | Activity | FAST Source | Workshop Target | Status | Priority | Notes |
|---|----------|-------------|-----------------|--------|----------|-------|
| 1 | Shared agent utils (auth, ssm, gateway, code_interpreter) | `patterns/utils/`, `tools/code_interpreter/` | `agent-code/shared/` | 🟢 Done | P0 | Extract user ID from JWT, SSM helpers, gateway MCP client, code interpreter tools |
| 2 | Strands agent with Gateway+Memory+CodeInterpreter | `patterns/strands-single-agent/basic_agent.py` | `agent-code/strands-agent/` | 🟢 Done | P0 | Replace bare agent with full FAST pattern |
| 3 | Sample gateway Lambda tool | `gateway/tools/sample_tool/` | `tools/sample_tool/` | 🟢 Done | P0 | text_analysis_tool with tool_spec.json, wired in app.py |
| 4 | Test scripts (agent, gateway, memory) | `test-scripts/` | `scripts/` | 🟢 Done | P0 | Adapted SSM paths to `/{project}/{env}/` convention |
| 5 | Fix IAM permissions (workload identity) | `infra-cdk/lib/utils/agentcore-role.ts` | `runtime_stack.py` | 🟢 Done | P0 | Added `GetWorkloadAccessTokenForJWT`, `GetWorkloadAccessTokenForUserId` |

### Tier 2 — Adapt Pattern

| # | Activity | FAST Source | Workshop Target | Status | Priority | Notes |
|---|----------|-------------|-----------------|--------|----------|-------|
| 6 | LangGraph single agent | `patterns/langgraph-single-agent/` | `agent-code/langgraph-agent/` | 🟢 Done | P1 | Alternative agent framework for Module 6 |
| 7 | Claude Agent SDK single agent | `patterns/claude-agent-sdk-single-agent/` | `agent-code/claude-sdk-agent/` | 🟢 Done | P1 | Alternative agent framework for Module 6 |
| 8 | Claude Agent SDK multi-agent | `patterns/claude-agent-sdk-multi-agent/` | `agent-code/claude-sdk-multi-agent/` | 🟢 Done | P1 | Multi-agent pattern for Module 8 (A2A) |
| 9 | AG-UI Strands agent | `patterns/agui-strands-agent/` | `agent-code/agui-strands-agent/` | 🟢 Done | P1 | AG-UI SSE streaming protocol, new Module B |
| 10 | AG-UI LangGraph agent | `patterns/agui-langgraph-agent/` | `agent-code/agui-langgraph-agent/` | 🟢 Done | P1 | AG-UI with LangGraph, new Module B |
| 11 | Pattern selection in app.py | N/A | `app.py` | 🟢 Done | P1 | CDK context var `agent_pattern` to select `source_dir` |
| 12 | Memory config flags (LTM toggle) | `config.yaml` | `stacks/memory_stack.py` | 🟢 Done | P1 | `use_long_term_memory`, `ltm_top_k`, `ltm_relevance_score` |
| 13 | Feedback API stack | `infra-cdk/lib/backend-stack.ts` (DynamoDB+APIGW+Lambda section) | `stacks/feedback_stack.py` | 🔴 Not Started | P2 | New workshop module D |
| 14 | Frontend stack (Amplify) | `infra-cdk/lib/amplify-hosting-stack.ts` | `stacks/frontend_stack.py` | 🔴 Not Started | P2 | S3 staging + Amplify App |
| 15 | Frontend source code | `frontend/` | `frontend/` | 🔴 Not Started | P2 | Copy React/Vite/Tailwind app wholesale |
| 16 | Docker Compose local dev | `docker/docker-compose.yml` | `docker/docker-compose.yml` | 🟢 Done | P2 | Local agent dev |
| 17 | GitHub Actions CI/CD | `.github/workflows/` | `.github/workflows/` | 🟢 Done | P2 | Security scan, Python lint, dependabot |
| 18 | GitLab CI | `.gitlab-ci.yml` | `.gitlab-ci.yml` | 🟢 Done | P2 | Ruff lint + ASH scanning |
| 19 | Makefile (lint/format) | `Makefile` | `Makefile` | 🟢 Done | P3 | ruff + deploy/synth/test targets |
| 20 | Dockerfile improvements | `patterns/*/Dockerfile` | `agent-code/*/Dockerfile` | 🟢 Done | P1 | Multi-stage builds, copy `shared/` dir |

### Tier 3 — Infrastructure Cleanup

| # | Activity | FAST Source | Workshop Target | Status | Priority | Notes |
|---|----------|-------------|-----------------|--------|----------|-------|
| 21 | Terraform infra | `infra-terraform/` | N/A | ⏭️ Skipped | — | Workshop is CDK-only |
| 22 | CodeBuild ephemeral deploy | `scripts/deploy-with-codebuild.py` | N/A | ⏭️ Skipped | — | We have `deploy.sh` |
| 23 | config.yaml / ConfigManager | `infra-cdk/lib/utils/config-manager.ts` | N/A | ⏭️ Skipped | — | CDK context approach is sufficient |

---

## Updated Workshop Module Map

| Module | Description | CDK Stacks | Agent Patterns Available |
|--------|-------------|------------|--------------------------|
| 3 | Infrastructure Blueprint | `auth` | — |
| 4 | Identity Integration | `auth`, `identity` | — |
| 5 | Gateway & Registry | `gateway` (with sample_tool) | — |
| 6 | Agent Deployment | `runtime-orchestrator` | strands / langgraph / claude-sdk (pick one) |
| 8 | Agent-to-Agent (A2A) | `runtime-code-agent`, `runtime-research-agent` | claude-sdk-multi-agent |
| 9 | Observability | `observability` | — |
| A | Memory | `memory` | — |
| B | AG-UI Streaming (NEW) | `runtime-orchestrator` (redeploy) | agui-strands / agui-langgraph |
| C | CI/CD Pipeline (NEW) | — | GitHub Actions / GitLab CI |
| D | Feedback & Frontend (NEW) | `feedback`, `frontend` | — |

---

## Updated File Structure

```
workshop-cdk/
├── app.py                          # CDK app entry — reads agent_pattern context
├── requirements.txt
├── cdk.json
├── scripts/
│   ├── deploy.sh
│   ├── test_agent.py               # From FAST test-scripts/
│   ├── test_gateway.py
│   └── test_memory.py
├── stacks/
│   ├── auth_stack.py
│   ├── identity_stack.py
│   ├── gateway_stack.py
│   ├── runtime_stack.py            # Reads agent_pattern to set source_dir
│   ├── memory_stack.py             # LTM config flags
│   ├── observability_stack.py
│   ├── networking_stack.py
│   ├── security_stack.py
│   ├── feedback_stack.py           # NEW — DynamoDB + APIGW + Lambda
│   └── frontend_stack.py           # NEW — Amplify hosting
├── infra_utils/
│   ├── agentcore_role.py           # Workload identity IAM helper
│   └── ...
├── agent-code/
│   ├── shared/                     # From FAST patterns/utils/
│   │   ├── auth.py
│   │   ├── ssm.py
│   │   ├── gateway.py
│   │   └── code_interpreter.py
│   ├── strands-agent/              # From FAST strands-single-agent
│   │   ├── basic_agent.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── langgraph-agent/            # From FAST langgraph-single-agent
│   │   ├── basic_agent.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── claude-sdk-agent/           # From FAST claude-agent-sdk-single-agent
│   │   ├── basic_agent.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── claude-sdk-multi-agent/     # From FAST claude-agent-sdk-multi-agent
│   │   ├── basic_agent.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── agui-strands-agent/         # From FAST agui-strands-agent
│   │   ├── basic_agent.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── agui-langgraph-agent/       # From FAST agui-langgraph-agent
│       ├── basic_agent.py
│       ├── Dockerfile
│       └── requirements.txt
├── tools/
│   └── sample_tool/                # From FAST gateway/tools/sample_tool
│       ├── lambda_function.py
│       └── tool_spec.json
├── frontend/                       # From FAST frontend/ (React/Vite/Tailwind)
├── docker/
│   └── docker-compose.yml          # From FAST docker/
├── dashboard/
│   ├── monitor.py
│   └── public/
├── docs/
│   ├── architecture.png
│   └── FAST_INTEGRATION_TRACKER.md
├── .github/
│   └── workflows/                  # From FAST .github/workflows/
├── .gitlab-ci.yml                  # From FAST
└── Makefile                        # From FAST — ruff + eslint + prettier
```

---

## Implementation Notes

### Pattern Selection

The `agent_pattern` CDK context variable controls which agent source directory is built and deployed by the runtime stack. This allows workshop participants to choose their preferred agent framework without changing infrastructure code.

```bash
# Deploy with the default Strands agent
cdk deploy RuntimeOrchestratorStack

# Deploy with LangGraph instead
cdk deploy RuntimeOrchestratorStack -c agent_pattern=langgraph-agent

# Deploy with Claude Agent SDK
cdk deploy RuntimeOrchestratorStack -c agent_pattern=claude-sdk-agent
```

The `runtime_stack.py` reads this context value and points `source_dir` at the corresponding `agent-code/{pattern}/` directory. If unset, it defaults to `strands-agent`.

### FAST Reference

Agent code is **adapted from FAST**, not copied verbatim. Python CDK stacks are original to this workshop — only the agent application code and shared utilities follow FAST patterns. All SSM parameter paths are remapped to our `/{project}/{environment}/` convention.

### Key Differences from FAST

| Aspect | FAST | Workshop CDK |
|--------|------|--------------|
| Stack architecture | Monolithic (single backend stack) | Modular (one stack per concern) |
| CDK language | TypeScript | Python |
| Runtime construct | L2 `Runtime` construct | L1 `CfnRuntime` with CodeBuild image build |
| Configuration | `config.yaml` + ConfigManager | CDK context (`-c key=value`) + env vars |
| Deploy tool | `deploy-with-codebuild.py` | `deploy.sh` (interactive + CI modes) |
| Workshop modules | N/A | Progressive module-by-module deployment |
