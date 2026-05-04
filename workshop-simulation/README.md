# AgentCore Workshop Simulation — Customer Walkthrough

This simulates a real 2-day workshop for a customer migrating an existing EC2-based agent to AgentCore with EntraID identity, gateway, A2A, and security.

## Customer Scenario

**ACME Corp** has:
- An existing Python agent running on EC2 (Strands-based)
- Microsoft Entra ID as their corporate IdP
- Wants to migrate to AgentCore for managed runtime, gateway, and A2A

## Prerequisites

- AWS CLI configured (account 506556589049, us-east-1)
- CDK CLI installed (`npm install -g aws-cdk`)
- Python 3.13+ with venv
- Docker running
- Entra ID app registration (tenant: 697a5720-ec4f-42dc-9713-d01182b20533)

## Workshop Modules

| Step | Module | What You Build | Time |
|------|--------|---------------|------|
| 1 | Infrastructure | Cognito + EntraID federation | 20 min |
| 2 | Identity (3LO) | OAuth credential providers | 10 min |
| 3 | Gateway | MCP Gateway + Lambda tools | 20 min |
| 4 | Agent Migration | Move EC2 agent → AgentCore Runtime | 30 min |
| 5 | A2A | Add code-agent + research-agent | 20 min |
| 6 | Security | KMS + CloudTrail hardening | 10 min |
| 7 | Observability | Vended logs + X-Ray | 10 min |

## Quick Start

```bash
# Run the full simulation (progressive deploy with pauses)
./run-workshop.sh

# Or run a specific module
./run-workshop.sh --module 3
```

## EntraID Configuration

| Setting | Value |
|---------|-------|
| Tenant ID | 697a5720-ec4f-42dc-9713-d01182b20533 |
| Client ID (Actor App) | 67111523-c2d9-47ae-a044-6fe6ef2c876a |
| Issuer URL | https://login.microsoftonline.com/697a5720-ec4f-42dc-9713-d01182b20533/v2.0 |

> **Note**: Client secret must be set via environment variable `IDP_CLIENT_SECRET` before running.
