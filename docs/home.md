<!-- markdownlint-disable MD041 -->

# Enterprise Agentic AI Platform Accelerator

A deployable platform for running AI agents under enterprise governance on
**Amazon Bedrock AgentCore** — authentication, identity, tool gateway, memory,
observability, and security controls, stood up from one declarative
configuration file in under an hour, in a single AWS account.

> Agents are easy to prototype and hard to run. This accelerator is the part
> that's hard: the governed platform your agents deploy onto.

## What you get

| Layer | What's deployed |
|---|---|
| **Identity** | Amazon Cognito with optional federation to your corporate IdP (Entra ID, Okta, Ping) |
| **Agents** | AgentCore Runtime hosting your framework — 7 patterns: Strands, LangGraph, Claude Agent SDK (single + multi), AG-UI, orchestrator |
| **Tools** | AgentCore Gateway (MCP): tools become governed platform citizens, not code baked into agents |
| **Memory** | AgentCore Memory — conversation state that survives restarts, with optional long-term memory |
| **Governance** | Model allow-list and guardrailed-only inference **enforced by IAM**, Cedar tool authorization, audit alerting |
| **Operations** | Vended logs, traces, CloudWatch alarms and a platform dashboard, plus a `verify` command that tests every claim your configuration makes |

## Two ways in

- **"I don't have a platform — how do I start fast?"** Deploy the greenfield
  profile and have a working, governed agent platform before lunch. Add
  security controls when security shows up; they're one config flag each.
- **"I have agents on other platforms — how do I migrate?"** Keep your
  framework. The migration profile lands your existing agent on AgentCore
  Runtime and decouples what it used to carry inside: tools to the Gateway,
  state to Memory, identity to your IdP, model choice to governed config.

Read [How it works](how-it-works.md), then head to
[Getting started](https://github.com/aws-samples/sample-agentcore-enterprise-platform#getting-started).

## What makes it different

Every capability this documentation claims has been **verified live** — the
platform ships a `verify` command that re-tests its own promises against your
deployment and exits non-zero on any failure. If the docs say inference without
a guardrail is denied, that denial is checked by IAM simulation on your actual
runtime role, not asserted in prose.

## Honest scope

This is a sample under active development, not an AWS product. Agent
*quality* evaluation and per-agent model cost attribution are on the roadmap,
not shipped. Run your own security review before production use.
