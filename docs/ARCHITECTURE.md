# Architecture

This is the AI landing zone as actually implemented by this repo, layer by
layer: governance (org SCPs from the control library), identity (Cognito +
AgentCore Identity Token Vault), platform services (gateway, memory, SSM
registry), agent workloads (runtimes + Bedrock), the deploy golden path,
observability, and the opt-in network layer. Solid arrows are the live request
flow verified end to end by `scripts/test.py` and `scripts/invoke.py`; dotted
arrows are opt-in features or control-plane relationships.

```mermaid
flowchart TB
    subgraph CLIENTS["Clients / Callers"]
        USER["Workshop user / App<br/>(OAuth: app, web clients)"]
        M2MC["Machine caller<br/>(M2M client_credentials)"]
    end
    subgraph GOV["Governance layer — org scope (Terraform, opt-in)"]
        SCP["Consolidated Gateway SCP (8 controls)<br/>+ Memory CMK SCP — 2 attachments/target"]
        CTLIB["control-library/<br/>single source: SCP · Cedar · IAM · VPCE · Guardrails"]
    end
    subgraph IDENTITY["Identity layer (auth + identity stacks)"]
        COG["Cognito User Pool<br/>3 clients · agentcore/invoke scope"]
        FED["Federated IdP (optional)<br/>Entra ID / Okta / Ping<br/>secret via {{resolve:secretsmanager}}"]
        M2MP["AgentCore Identity<br/>OAuth2 credential provider<br/>gateway-m2m (Token Vault)"]
    end
    subgraph PLATFORM["Platform services layer"]
        GW["AgentCore Gateway (MCP, CUSTOM_JWT)<br/>opt-in: Cedar policy engine (LOG_ONLY)<br/>opt-in: Guardrail + egress interceptor λ"]
        TOOL["Lambda tool target(s)<br/>sample-tool"]
        MEM["AgentCore Memory<br/>semantic + user-preference<br/>opt-in: KMS CMK · resource policy"]
        SSM["SSM Parameter Store<br/>/{project}/{env}/* — cross-stack registry"]
    end
    subgraph WORKLOAD["Agent workload layer (runtime stacks)"]
        RT["Runtime: orchestrator (HTTP)<br/>env: MODEL_ID · GATEWAY_URL ·<br/>GATEWAY_CREDENTIAL_PROVIDER_NAME · MEMORY_ID"]
        A2A["A2A runtimes (opt-in)<br/>code-agent · research-agent"]
        BR["Bedrock<br/>us.anthropic.claude-sonnet-4-6<br/>(env-overridable via MODEL_ID)"]
    end
    subgraph FACTORY["Golden path (deploy)"]
        DEP["deploy.sh wizard<br/>profiles · modules · teams"]
        CB["CodeBuild ARM64<br/>source-hash triggered"]
        ECR["ECR per component"]
    end
    subgraph OBS["Observability layer"]
        LOGS["Vended logs + X-Ray<br/>+ dashboard/monitor.py"]
        TRAIL["CloudTrail + KMS (opt-in)<br/>SNS alerting (opt-in)"]
    end
    subgraph NET["Network layer (opt-in)"]
        VPC["VPC + endpoints<br/>gateway VPCE policy:<br/>OAuth pass-through + SigV4 org-lock"]
    end
    USER -->|"OIDC login (opt. federated)"| COG
    FED -.-> COG
    M2MC -->|client_credentials| COG
    COG -->|"JWT bearer"| RT
    RT --> BR
    RT -->|"M2M token request"| M2MP
    M2MP -->|"client_credentials"| COG
    M2MP -->|"gateway JWT"| RT
    RT -->|"MCP + JWT"| GW
    GW --> TOOL
    RT <-->|"session + semantic recall"| MEM
    RT -.->|A2A protocol| A2A
    DEP --> CB --> ECR --> RT
    SCP -.->|constrains Create/Update| GW
    SCP -.->|CMK required| MEM
    CTLIB -.-> SCP
    VPC -.->|private path| GW
    RT --> LOGS
    GW --> LOGS
    SSM -.-> RT
```

## Request flows

The three solid-arrow flows below are verified end to end against a live
deployment:

1. **User JWT invoke** — a caller gets a JWT from Cognito (OIDC login or M2M
   `client_credentials` with the `agentcore/invoke` scope) and POSTs the prompt
   to the orchestrator runtime's data-plane endpoint as a Bearer token
   (`scripts/test.py`, `scripts/invoke.py`).
2. **Agent M2M token via Token Vault → gateway MCP** — the runtime requests an
   M2M token from the AgentCore Identity `gateway-m2m` credential provider
   (Token Vault), which runs `client_credentials` against Cognito and hands the
   gateway JWT back; the runtime then calls the gateway over MCP with that JWT,
   and the gateway invokes its Lambda tool targets
   (`scripts/invoke.py --tools`, `scripts/test_gateway.py`).
3. **Deploy golden path** — `scripts/deploy.sh` drives CDK; each runtime stack
   owns an ECR repo and a source-hash-triggered ARM64 CodeBuild that produces
   the container the runtime runs.

## Reading the diagram, layer by layer

- **Governance (org scope, opt-in):** `terraform/org-guardrails/` attaches two SCPs
  per target — the consolidated Gateway SCP (8 controls) and the Memory CMK SCP.
  Both are generated from `control-library/`, the single source of truth for SCP,
  Cedar, IAM, VPCE, and Guardrail policy documents.
- **Identity:** the `auth` stack creates one Cognito User Pool with 3 app clients
  and the `agentcore/invoke` resource-server scope; a federated IdP (Entra ID,
  Okta, Ping) is optional and wired via `{{resolve:secretsmanager}}`. The
  `identity` stack registers the `gateway-m2m` OAuth2 credential provider in the
  AgentCore Identity Token Vault so the runtime can fetch M2M tokens itself.
- **Platform services:** the `gateway` stack exposes Lambda tool targets over MCP
  with CUSTOM_JWT auth (opt-in Cedar policy engine in LOG_ONLY mode, opt-in
  Guardrail + egress interceptor Lambda); the `memory` stack provisions semantic
  and user-preference strategies (opt-in KMS CMK + resource policy). Every stack
  publishes its outputs to the SSM `/{project}/{env}/*` registry.
- **Agent workload:** the orchestrator runtime (HTTP protocol) reads its wiring
  from environment variables set by the runtime stack, calls Bedrock
  (`us.anthropic.claude-sonnet-4-6` by default, env-overridable via `MODEL_ID`),
  and talks to the gateway with an M2M JWT it obtains through the credential
  provider. A2A sub-agent runtimes (code-agent, research-agent) are opt-in.
- **Golden path:** `scripts/deploy.sh` (profiles, modules, teams) drives CDK;
  each runtime stack owns an ECR repo and a source-hash-triggered ARM64 CodeBuild
  that produces the container the runtime runs.
- **Observability:** runtimes and the gateway emit vended logs and X-Ray traces;
  `dashboard/monitor.py` polls status for the local dashboard. CloudTrail with a
  KMS key and SNS alerting are opt-in via the `security` stack.
- **Network (opt-in):** the `networking` stack adds a VPC and endpoints; the
  gateway VPC endpoint policy allows OAuth pass-through while org-locking SigV4
  calls.
