# Repository structure

This repository is a capability-oriented monorepo. A directory should group
one product responsibility or one deployable artifact, not one implementation
language. The structure favors discoverability for workshop participants over
adding a generic `src/` layer.

## Current layout

```text
.
├── app.py                   CDK composition root
├── stacks/                  deployable CDK stack units
├── infra_utils/             shared configuration and infrastructure helpers
├── agent-code/              independently built runtime images
├── migration/               adapter, network probe, and EBA simulation
├── control-library/         controls and their organization deployment module
├── use-cases/               opt-in product integrations
├── dashboard/               loopback EBA Console
├── tools/                   sample Gateway tools and interceptors
├── presets/                 reviewed platform.yaml starting points
├── scripts/                 deployment, verification, and developer commands
├── tests/                   repository-level automated tests
└── docs/                    customer and contributor documentation
```

`.github/`, `.agentcore/`, and `docker/` contain repository automation,
AgentCore CLI configuration, and local container composition respectively.

## Where a change belongs

| Change | Location | Contract |
|---|---|---|
| New runtime framework or agent pattern | `agent-code/<pattern>/` | `Dockerfile`, `requirements.txt`, and runtime entry point |
| Platform-wide AWS capability | `stacks/` plus focused helpers in `infra_utils/` | Declared deployment contract and synthesis tests |
| Customer/product integration | `use-cases/<name>/` | `manifest.yaml`, `stack.py`, `verify.py`, and `walkthrough.md` |
| Gateway target or interceptor | `tools/<name>/` | Tool specification and least-privilege stack integration |
| Security control | `control-library/<type>/` | Catalog entry, valid policy source, and control validation |
| Existing-agent migration behavior | `migration/<component>/` | Opt-in migration configuration and migration tests |
| Operator command | `scripts/` | Bounded command interface and automated test |
| Customer-facing explanation | `docs/` | Documentation integrity check and sidebar entry when navigable |

Do not add a top-level directory merely because a component uses a different
language or deployment tool. Add it to the closest capability root. A genuinely
new product responsibility requires an update to this document and to
`scripts/check_repository_structure.py` in the same pull request.

## Dependency direction

```text
app.py ──composes──> stacks/ ──uses──> infra_utils/
                         │
                         └──packages──> agent-code/, migration/, tools/

use-cases/ ──consume──> platform SSM/OAuth/Gateway interface
dashboard browser ──consumes──> sanitized status and invocation contracts
```

- `app.py` is the composition root; reusable behavior does not belong there.
- A stack may use `infra_utils`, but `infra_utils` must not import stacks.
- Runtime code must not import infrastructure packages.
- Use cases must not import `stacks`; they consume the documented
  [platform interface](PLATFORM_INTERFACE.md).
- Dashboard browser code must not depend on CDK objects or expose raw
  CloudFormation outputs.
- Scripts orchestrate these public contracts; they must not become a second
  implementation of platform behavior.

## Change protocol

For a structural change:

1. Keep the change responsibility-preserving; do not combine it with behavior
   changes.
2. Move all code, tests, CI paths, presets, and documentation in one pull
   request.
3. Preserve configuration keys, resource names, and external interfaces.
4. Run `make check-structure`, the repository tests, documentation checks,
   shell checks, and synthesis/contract parity when deployment paths move.
5. Record the decision and evidence in `memory.md`.

The top-level `terraform` symlink is a deprecated compatibility alias for
existing module source references. New configurations use
`control-library/terraform/org-guardrails`; remove the alias only in a declared
breaking release.

Local build output and editor state such as `.venv/`, `cdk.out/`,
`.pytest_cache/`, `.ruff_cache/`, `.worktrees/`, `.vscode/`, and
`workshop.env` are intentionally ignored and are not part of the repository
structure.
