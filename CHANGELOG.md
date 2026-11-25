# Changelog

All notable changes to the accelerator are recorded here.

## [Unreleased]

### Changed

- `identity.mode` now defaults per IdP when omitted: `direct` for `entra_id`,
  `brokered` for `cognito`, `okta` and `ping` (Okta and Ping keep brokered
  until their direct mode exists). An explicit `mode` is always honoured.
- `presets/production.yaml` and `presets/federated.yaml` pin
  `identity.mode: direct` explicitly.

### Upgrade note

- Existing `entra_id` deployments whose `platform.yaml` omits `identity.mode`
  were brokered and will switch to direct on the next build. To keep them
  brokered, pin it:

  ```yaml
  identity:
    idp: entra_id
    mode: brokered
  ```

  The same applies to `okta` once Okta direct becomes its default.

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
