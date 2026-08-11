# AgentCore Workshop Simulation — Customer Walkthrough

This simulates a real 2-day workshop for a customer migrating an existing EC2-based agent to AgentCore with EntraID identity, gateway, A2A, and security.

## Customer Scenario

**ACME Corp** has:
- An existing Python agent running on EC2 (Strands-based)
- Microsoft Entra ID as their corporate IdP
- Wants to migrate to AgentCore for managed runtime, gateway, and A2A

## Prerequisites

- AWS CLI configured with credentials for your workshop account
- CDK CLI installed (`npm install -g aws-cdk`)
- Python 3.13+ with venv
- Docker running
- An Entra ID app registration in your tenant (tenant ID, client ID, client secret)

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

Set your Entra ID app registration values as environment variables before running
(`run-workshop.sh` fails fast if any are missing):

```bash
export IDP_TENANT_ID='<your-entra-tenant-id>'
export IDP_CLIENT_ID='<your-entra-app-client-id>'
export IDP_CLIENT_SECRET='<your-entra-client-secret>'
```

| Setting | Value |
|---------|-------|
| Tenant ID | `$IDP_TENANT_ID` — from Entra ID → App registrations → Overview |
| Client ID (Actor App) | `$IDP_CLIENT_ID` — the app registration's Application (client) ID |
| Issuer URL | `https://login.microsoftonline.com/<your-entra-tenant-id>/v2.0` (derived automatically) |
