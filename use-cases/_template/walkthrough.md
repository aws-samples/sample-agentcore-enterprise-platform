# {{name}}

{{summary}}

## What it deploys

One stack, `<project>-<env>-uc-{{name}}`, which reads the platform's gateway URL
from the SSM interface and publishes it back under this use case's own namespace,
`/<project>/<env>/use-cases/{{name}}/gateway-seen`. That is the scaffold; replace
it with what your use case needs and keep the shape: consume the platform through
SSM, publish under your namespace.

## Enable, build, verify

```bash
# platform.yaml already names it (deploy.sh usecase new added the entry):
#   use_cases:
#     {{name}}: {}
./scripts/deploy.sh design      # the plan now lists uc-{{name}}
./scripts/deploy.sh build       # deploys the platform and this stack
./scripts/deploy.sh verify      # runs use-cases/{{name}}/verify.py last
```

## Configuration

Anything under `use_cases: {{name}}:` in `platform.yaml` reaches `build()` as the
`config` dict. The scaffold reads one optional key, `label`.

## Remove

Delete the `{{name}}:` line from `use_cases:` and run `./scripts/deploy.sh destroy
--stack <project>-<env>-uc-{{name}}`; the folder can stay or go.
