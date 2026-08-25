# hello-platform — reference use case

The smallest possible use case: one zero-cost SSM parameter proving your
deployment's extension point works, and the folder you copy to build a real
one. See `CONTRIBUTING_USE_CASES.md` for the contribution rules and
`docs/PLATFORM_INTERFACE.md` for what a use case may consume.

## Enable

In `platform.yaml` (any preset works as a base — the use case requires the
gateway, which every centralized footprint has):

```yaml
use_cases:
  hello-platform:
    greeting: ciao        # optional — your use case defines its own keys
```

An unknown name under `use_cases:` is a validation error listing what is
available, so a typo cannot silently deploy nothing.

## Deploy

The stack rides the normal flow — it appears in `deploy.sh ls`, in the
full-footprint plan, and in the contract (`python -m infra_utils.platform_config
--stacks platform.yaml`):

```bash
./scripts/deploy.sh deploy --stack <project>-<env>-uc-hello-platform
```

## Verify

```bash
python use-cases/hello-platform/verify.py
# OK: /<project>/<env>/use-cases/hello-platform/gateway-seen = ciao: https://...
```

## What to look at before copying

- `manifest.yaml` — the only file the platform reads about you.
- `stack.py` — consumes the gateway URL through the SSM interface at deploy
  time; publishes its own output back under `use-cases/<name>/`.
- `verify.py` — exits non-zero on any failed claim.
