# Changelog

All notable changes to the accelerator are recorded here.

## [Unreleased]

### Changed

- `identity.mode` now defaults per IdP when omitted: `direct` for `entra_id`,
  `brokered` for `cognito`, `okta` and `ping`. Okta direct is supported
  (live evidence pending) but stays opt-in; Ping has no direct mode. An
  explicit `mode` is always honoured.
- The synth region now follows `-c region`, then `platform.yaml` `region`;
  `CDK_DEFAULT_REGION` only fills a missing value.
- Synthesis refuses `security.cedar.mode: ENFORCE` with
  `security.cedar.enabled: false`, an empty `gateway.tools` list, and unknown
  bare tool names. ENFORCE with gateway tools the deployed Cedar permit
  (`cedar.gateway-default.permit-read`) does not cover (for example web
  search) warns in workshop mode and is refused in production; coverage is
  read from that policy, so adding a tool's actions there is enough.
- Gateway and runtime IAM ARNs use the stack partition instead of a
  hard-coded `aws`.
- `docs/POSTURE_TIERS.md` documents the `dev`, `hardened` and `production`
  posture layers, generated from `POSTURES`.
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
