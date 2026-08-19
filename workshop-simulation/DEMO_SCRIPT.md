# AgentCore Platform & Security Accelerator — Workshop Demo Script

## Overview

This document is the **facilitator's run script** for the 2-day AgentCore workshop. It simulates a real customer engagement where ACME Corp migrates their existing EC2-based agent to AgentCore with enterprise identity, governance, and multi-agent architecture.

## Customer Profile: ACME Corp

| Attribute | Value |
|-----------|-------|
| Industry | Financial Services |
| IdP | Microsoft Entra ID |
| Current State | Python agent on EC2, no auth, no governance |
| Target State | AgentCore Runtime + Gateway + A2A + Memory |
| Security Req | KMS encryption, CloudTrail audit, federated identity |
| Teams | Platform (infra), Agent (developers), Security (compliance) |

## Team Structure

| Team | Members | Responsibility | Stacks |
|------|---------|---------------|--------|
| Platform | 2-3 | Networking, auth, gateway, observability | auth, identity, gateway, observability |
| Agent | 2-3 | Agent code, runtime, memory, A2A | runtime-*, memory |
| Security | 1-2 | KMS, CloudTrail, policy review | security, observability |

## Pre-Workshop Setup (Facilitator)

```bash
# Destroy any existing deployment
cd workshop-simulation
./run-workshop.sh --destroy

# Verify clean slate
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE --query "StackSummaries[?contains(StackName,'agentcore-workshop')].StackName"
# Should return []
```

**Required secrets:**
```bash
export IDP_CLIENT_SECRET='<entra-actor-app-secret>'  # From Azure portal or Secrets Manager
```

---

## Day 1: Foundation & Migration

### Module 1: Infrastructure Blueprint (Platform Team) — 20 min

**Narrative:** "ACME uses Microsoft Entra ID. We need Cognito as the token broker between Entra and AgentCore."

**What to show:**
1. `stacks/auth_stack.py` — walk through the Cognito + OIDC federation code
2. Explain the 3 client types: app (auth code), web (SRP), m2m (client_credentials)
3. Show the resource server with `agentcore/invoke` scope

**Deploy:**
```bash
# The secret goes to Secrets Manager first — CDK only ever receives its NAME.
aws secretsmanager create-secret --name agentcore-workshop-dev-idp-client-secret \
  --secret-string "$IDP_CLIENT_SECRET"

cdk deploy agentcore-workshop-dev-auth \
  -c idp_type=entra_id \
  -c idp_tenant_id=$IDP_TENANT_ID \
  -c idp_client_id=$IDP_CLIENT_ID \
  -c idp_client_secret_name=agentcore-workshop-dev-idp-client-secret
```

`run-workshop.sh` does both steps for you. Passing the secret itself as
`-c idp_client_secret=...` is rejected by the stack: context values land in
`cdk.context.json`, process listings, and the synthesized template.

**Verify:**
- Open Cognito console → User Pool → Sign-in experience → show EntraID provider
- Show SSM parameters: `aws ssm get-parameters-by-path --path /agentcore-workshop/dev/auth --recursive`

**Key talking points:**
- Cognito wraps Entra tokens → AgentCore sees a standard OIDC token
- M2M client for service-to-service (no user context)
- App client for user-facing flows (auth code + PKCE)

---

### Module 2: Identity & 3LO (Platform Team) — 10 min

**Narrative:** "Agents need to act on behalf of users with external services — Google Calendar, GitHub, etc."

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-identity
```

**What to explain:**
- OAuth2 Credential Providers in AgentCore Identity
- 3LO = Three-Legged OAuth (user consent flow)
- For this demo, no external providers configured (would need Google/GitHub app registrations)
- In production: agent calls `GetResourceOauth2Token` to get user-scoped tokens

---

### Module 3: Gateway & Tool Registry (Platform Team) — 20 min

**Narrative:** "Instead of agents calling tools directly, we put a gateway in front. Centralized auth, audit, and discovery."

**What to show:**
1. `stacks/gateway_stack.py` — Gateway + Lambda tool target
2. `tools/sample_tool/handler.py` — the Lambda behind the gateway
3. The MCP protocol (JSON-RPC 2.0)

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-gateway
```

**Live test (do this in front of the audience):**
```python
# Get M2M token
# Call tools/list → shows text_analysis_tool
# Call tools/call → invoke the tool
python scripts/test_gateway.py
```

**Key talking points:**
- Gateway validates JWT before forwarding to tool
- Tools are Lambda functions — serverless, auto-scaling
- MCP protocol = standard interface, any agent framework can use it
- Cedar policies (future) for fine-grained tool access control

---

### Module 4: Memory (Agent Team) — 15 min

**Narrative:** "ACME's EC2 agent has no memory. Every conversation starts fresh. Let's fix that."

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-memory
```

**What to explain:**
- Semantic memory: vector-based recall across sessions
- User preference memory: learns patterns (e.g., "user prefers Python")
- Memory is per-user (actor_id from JWT sub claim)
- No infrastructure to manage — fully managed by AgentCore

---

### Module 5: Agent Migration — EC2 → AgentCore Runtime (Agent Team) — 30 min

**Narrative:** "Here's ACME's existing agent. It runs on EC2. Let's migrate it to AgentCore."

**Show the "before" code:**
```bash
cat workshop-simulation/existing-ec2-agent/agent.py
```

**Explain what changes:**

| Before (EC2) | After (AgentCore Runtime) |
|--------------|--------------------------|
| Manual EC2 instance | Managed container runtime |
| No auth | Cognito + EntraID built-in |
| No tool governance | Gateway for all tool access |
| No memory | Managed memory (semantic + preferences) |
| Manual scaling | Auto-scaling |
| No observability | Vended logs + X-Ray |

**Show the "after" code:**
```bash
cat agent-code/strands-agent/agent.py
cat agent-code/strands-agent/Dockerfile
```

**Key changes:**
1. Add `Dockerfile` (containerize)
2. Use `BedrockAgentCoreApp` as the entrypoint
3. Wire memory via env vars (`MEMORY_ID`)
4. That's it — agent logic stays the same

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-runtime-orchestrator -c agent_pattern=strands-agent
```

**Live test:**
```bash
# Invoke the agent
python scripts/test_agent.py --component orchestrator
# Ask: "What tools do you have?"
# Ask: "Analyze this text: AgentCore is awesome"
```

---

## Day 2: Multi-Agent & Enterprise

### Module 6: Agent-to-Agent Communication (Agent Team) — 20 min

**Narrative:** "Instead of one monolithic agent, ACME wants specialized agents that collaborate."

**Architecture:**
```
User → Orchestrator (HTTP, JWT auth)
           ├── Code Agent (A2A, SigV4 auth)
           └── Research Agent (A2A, SigV4 auth)
```

**Key difference: A2A uses SigV4, not JWT**
- HTTP runtimes: user-facing, JWT auth (Cognito token)
- A2A runtimes: service-to-service, SigV4 auth (IAM)
- The orchestrator calls sub-agents using its IAM role

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-runtime-code-agent agentcore-workshop-dev-runtime-research-agent \
  -c enable_a2a=true
```

**What to explain:**
- Each agent has its own container, scaling, and lifecycle
- Orchestrator decides what to delegate
- A2A protocol handles routing and response aggregation
- Each agent can have different models, tools, and permissions

---

### Module 7: Security Hardening (Security Team) — 15 min

**Narrative:** "ACME's security team requires encryption at rest with customer-managed keys and full audit trail."

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-security -c enable_security=true
```

**What to show:**
- KMS CMK creation and key policy
- CloudTrail configuration for AgentCore API calls
- How the memory stack references the KMS key for encryption

**Discussion points:**
- VPC endpoints (optional, for network isolation)
- Cedar policies for tool-level access control
- Cross-account patterns (hub-and-spoke)

---

### Module 8: Observability (Platform + Security Teams) — 10 min

**Narrative:** "We need visibility into every agent invocation, tool call, and error."

**Deploy:**
```bash
cdk deploy agentcore-workshop-dev-observability -c enable_a2a=true
```

**What to show:**
- Vended log delivery per resource (gateway, memory, each runtime)
- CloudWatch Logs Insights query:
  ```
  fields @timestamp, @message
  | filter @logStream = "otel-rt-logs"
  | sort @timestamp desc
  | limit 20
  ```
- X-Ray trace for a full request flow

---

## Post-Workshop: Full Stack Verification

Run the complete test suite:

```bash
# Auth + Gateway
python3 -c "
import boto3, requests, json, base64
# ... (M2M token + tools/list + tools/call)
"

# Orchestrator
python scripts/test_agent.py --component orchestrator

# Dashboard
cd dashboard && python3 monitor.py &
python3 -m http.server 8888 -d public
# Open http://localhost:8888
```

---

## Timing Guide

| Time | Module | Team | Duration |
|------|--------|------|----------|
| 09:00 | Kickoff + Architecture Overview | All | 30 min |
| 09:30 | Module 1: Auth + EntraID | Platform | 20 min |
| 09:50 | Module 2: Identity (3LO) | Platform | 10 min |
| 10:00 | Break | — | 15 min |
| 10:15 | Module 3: Gateway | Platform | 20 min |
| 10:35 | Module 4: Memory | Agent | 15 min |
| 10:50 | Module 5: Agent Migration | Agent | 30 min |
| 11:20 | Day 1 Wrap-up + Q&A | All | 10 min |
| — | — | — | — |
| 09:00 | Day 2 Kickoff | All | 10 min |
| 09:10 | Module 6: A2A | Agent | 20 min |
| 09:30 | Module 7: Security | Security | 15 min |
| 09:45 | Module 8: Observability | Platform + Security | 10 min |
| 09:55 | Break | — | 15 min |
| 10:10 | Hands-on: Participants deploy their own | All | 60 min |
| 11:10 | Architecture Review + Next Steps | All | 20 min |

---

## Common Issues & Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Runtime 424 | Container cold start | Wait 60-90s, retry. First invocation after deploy takes time. |
| Runtime 424 persistent | Container crash at startup | Check CloudWatch logs. Usually import error or missing env var. |
| Gateway 401 | Token expired or wrong audience | Re-fetch M2M token. Check `allowedClients` in gateway config. |
| A2A 403 | Using JWT instead of SigV4 | A2A runtimes require SigV4 auth, not Bearer tokens. |
| CDK deploy fails | Missing context vars | Ensure all `-c` flags are passed (idp_type, tenant_id, etc.) |

---

## Key Architecture Decisions

1. **Cognito as token broker** — not direct EntraID to AgentCore. Gives us standard OIDC + multiple client types.
2. **M2M for service-to-service** — client_credentials grant, no user context needed for gateway/tool calls.
3. **SigV4 for A2A** — sub-agents are internal services, IAM is the right auth boundary.
4. **Strands as agent framework** — lightweight, streaming, good AgentCore integration.
5. **Progressive deployment** — each module is independent, deploy in any order (respecting dependencies).
