# Changelog

All notable changes to the accelerator are recorded here.

## [0.1.0] - 2026-09-21

Initial numbered release.

### Added

- Declarative `platform.yaml` design with centralized, distributed, and
  federated deployment strategies.
- Guided workshop and fail-closed production lifecycle modes.
- AgentCore Runtime, Gateway, Identity, Memory, observability, networking, and
  security stack modules.
- Seven agent framework patterns, use-case scaffolding, configuration-aware
  verification, and the local deployment dashboard.
- Entra ID, Okta, and Ping federation through Cognito, plus direct Entra ID
  issuer mode.
- Evidence-gated migration of compatible arm64 containers to AgentCore Runtime
  through the supplied adapter.
- Read-only `deploy.sh doctor` customer preflight for tools, AWS target,
  Secrets Manager references, Bedrock model metadata, and migration images.

### Security and reliability

- Pinned deployment accounts, fail-closed configuration validation, secret
  references instead of manifest values, and resumable M2M credential rotation.
- Optional model allow-listing, Bedrock Guardrails, Cedar policy enforcement,
  VPC isolation, resource policies, CloudTrail, traceability alerts, and
  customer-managed encryption.
- Retained stateful resources in production mode and bounded Memory/log
  retention configuration.

### Documentation

- EBA participant and migration runbooks, module references, production
  readiness templates, a support matrix, known limitations, and release notes.

[0.1.0]: https://github.com/aws-samples/sample-agentcore-enterprise-platform/releases/tag/v0.1.0
