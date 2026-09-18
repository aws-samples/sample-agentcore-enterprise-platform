# Use cases

A use case is what a customer builds on top of the platform: one folder here,
one stack (or more), consuming the platform through its SSM interface and
enabled by one line in `platform.yaml`. The platform never imports a use case
and a use case never imports a core stack; the contract between them is
`docs/PLATFORM_INTERFACE.md`.

```
use-cases/<name>/
  manifest.yaml     what the platform reads: name, owner, requires, stacks, entry
  stack.py          build(app, ctx, config) — your CDK
  verify.py         REQUIRED: one real invocation; OK/exit 0 or FAIL/exit 1
  walkthrough.md    how to enable, build, verify, remove
```

Start one:

```bash
./scripts/deploy.sh usecase new release-notes --summary "Release notes from Jira transitions"
./scripts/deploy.sh design    # the plan now lists uc-release-notes
./scripts/deploy.sh build
./scripts/deploy.sh verify    # runs use-cases/release-notes/verify.py last
./scripts/deploy.sh usecase list
```

`hello-platform/` is the reference; `_template/` is what `usecase new` copies.
The rules for contributing one are in `CONTRIBUTING_USE_CASES.md`.
