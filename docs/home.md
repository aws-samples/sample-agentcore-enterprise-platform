<!-- markdownlint-disable MD041 -->

# Agentic AI Platform EBA

The Agentic AI Platform EBA (Experience-Based Acceleration) is a deployable
platform for running AI agents under enterprise governance on
**Amazon Bedrock AgentCore** — authentication, identity, tool gateway, memory,
observability, and security controls, driven by one declarative configuration
file. The default workshop path is designed to stand up in under an hour in
one AWS account; multi-account and migration paths have additional customer
prerequisites. Your team designs it, builds it and a first use case on it,
verifies it, and owns it.

> Agents are easy to prototype and hard to run. This accelerator is the part
> that's hard: the governed platform your agents deploy onto.

## What you get

| Layer | What's deployed |
|---|---|
| **Identity** | Your choice, declared in Design: Amazon Cognito as the token issuer with your corporate IdP (Entra ID, Okta, Ping) federated in — or Entra ID as the issuer directly, with no Cognito deployed |
| **Agents** | AgentCore Runtime hosting your framework — 7 patterns: Strands, LangGraph, Claude Agent SDK (single + multi), AG-UI, orchestrator |
| **Tools** | AgentCore Gateway (MCP): tools become governed platform citizens, not code baked into agents |
| **Memory** | AgentCore Memory — conversation state that survives restarts, with optional long-term memory |
| **Governance** | Model allow-list and guardrailed-only inference **enforced by IAM**, Cedar tool authorization, audit alerting |
| **Operations** | Vended logs, traces, CloudWatch alarms and a platform dashboard, plus a `verify` command that tests every claim your configuration makes |

## Design → Build → Verify

1. **Design.** Pick a profile, or write your own `platform.yaml`: single or
   multi-account, your IdP and who issues tokens, your agent framework, the
   controls you need. `deploy.sh usecase new <name>` scaffolds your
   application into the same file. `deploy.sh design` validates it and prints
   the exact stacks it produces — nothing is deployed yet.
2. **Build.** `deploy.sh build` stands up the platform and every use case in
   the design, with one command.
3. **Verify.** `deploy.sh verify` re-tests every promise the design made,
   platform and use cases alike, and exits non-zero on any failure.

## Two ways in

- **"I don't have a platform — how do I start fast?"** Design from the
  greenfield profile and have a working, governed agent platform before lunch.
  Select the required security controls during design; each one is an explicit
  configuration flag that also drives verification.
- **"I have agents on other platforms — how do I migrate?"** Keep your
  existing container and framework. The migration profile places a compatible
  arm64 container behind an adapter on AgentCore Runtime. It does not
  automatically extract tools or state, create private connectivity, copy
  customer data, or move triggers and traffic. Those changes remain explicit,
  customer-operated stages with evidence and approval gates. Follow the
  [EBA migration runbook](MIGRATION_RUNBOOK.md).

Read [How it works](how-it-works.md), then head to
[Getting started](https://github.com/aws-samples/sample-agentcore-enterprise-platform#getting-started).

## What makes it different

Core accelerator-managed paths have been **verified live** in the repository's
development environment. The platform ships a `verify` command that re-tests
the promises selected by your configuration and exits non-zero on failure. If
the docs say inference without a guardrail is denied, that denial is checked
by IAM simulation on your actual runtime role, not asserted in prose.

Customer-owned migration work—data procedures, existing private connectivity,
event sources, and traffic routing—is deliberately not executed by the
accelerator. Its readiness commands validate the recorded plan, evidence,
rollback, and approvals; the customer remains responsible for execution and
acceptance.

## Honest scope

This is a sample under active development, not an AWS product. Agent
*quality* evaluation and per-agent model cost attribution are on the roadmap,
not shipped. Run your own security review before production use.
