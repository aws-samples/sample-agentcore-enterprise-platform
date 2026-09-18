# Contributing a use case

A use case is a self-contained product integration — a desktop MCP client
fleet, an Amazon Connect hookup, an internal-API bridge — that customers turn
on with a few lines of `platform.yaml` after the platform itself is deployed.
You own your folder; the platform guarantees the interface.

## Is a use case the right shape for your contribution?

- **A new agent framework or pattern** → `agent-code/<pattern>/`, following an
  existing pattern (Dockerfile + agent module, selected via `agent_pattern`).
- **A platform capability** (changes auth, gateway, memory, networking for
  everyone) → a core stack. Open an issue first — core changes need
  coordination the use-case path deliberately avoids.
- **A product integration on top of the platform** → this document.

## The shape

Copy `use-cases/hello-platform/` and keep all four parts:

```
use-cases/<your-name>/
  manifest.yaml     # the ONLY file the platform reads about you (validated)
  stack.py          # CDK; build(app, ctx, config) is the entry point
  verify.py         # REQUIRED: one real invocation; OK/exit 0 or FAIL/exit 1
  walkthrough.md    # enable → deploy → verify, written for a participant
```

`manifest.yaml` declares your identity (`name` must match the folder), your
`owner` (you review changes here), what you `require` from the platform
footprint (core stack suffixes like `gateway`, `auth`), and the `stacks` you
add — which must carry the `uc-` prefix so a contribution can never collide
with a core stack.

## The rules the tooling enforces

- Nothing deploys unless `platform.yaml` names your use case under
  `use_cases:`. Opt-in is not a convention here; it is the mechanism.
- Your `requires` are checked against the actual footprint per federation
  role — a gateway-requiring use case cannot be enabled in an account with no
  local gateway; the error says which side of a federation it belongs on.
- Your stacks appear in the deployment contract (`expected_stacks()`), the
  full-footprint plan, the parity gate in CI, and the destroy path — for
  free, because the contract is the single source everything consumes.
- Consume the platform via `docs/PLATFORM_INTERFACE.md` (SSM parameters +
  Cognito tokens), never by importing core stacks. `stack.py` gets a small
  context dict (project, environment, prefix, ssm_prefix, region, cdk_env) —
  that and SSM is everything you need.

## The verify contract

`verify.py` is REQUIRED (a test fails when a discovered use case has none),
and `deploy.sh verify` runs it automatically — after every core check, so a
broken platform fails on the platform's own check, not on yours — whenever
your use case is enabled in `platform.yaml`.

- It MUST perform one real invocation of what the use case deploys: call the
  Lambda, hit the endpoint, ask the gateway for its tools. Checking that a
  resource exists only proves CloudFormation ran; the point is to prove the
  thing works. `hello-platform/verify.py` is the reference: it takes the URL
  out of the parameter it published and actually calls that gateway.
- Exit `0` and print one `OK: ...` line on success; exit `1` and print
  `FAIL: <cause>` otherwise. When the stack is absent, when there are no
  credentials, when the network is down: `FAIL:`, never a traceback (a test
  runs every `verify.py` with a scrubbed environment and expects exactly that,
  within 20 seconds).
- It receives the same environment as `deploy.sh verify`: AWS credentials,
  `PROJECT_NAME`, `ENVIRONMENT`, `AWS_REGION`, plus the `platform.yaml` values
  exported by `scripts/verify.py`. Read those; do not hardcode a project name.
- It runs with `scripts/` as the working directory. Import the helpers there
  rather than copying them (`sys.path.insert(0, <repo>/scripts)`, then
  `from utils import get_m2m_token`, `from test_gateway import mcp_request`).
- It MUST also work standalone: `python use-cases/<name>/verify.py` — that is
  what your walkthrough tells participants to run.

## The rules a reviewer enforces

- `verify.py` must be able to FAIL. A verify that prints success
  unconditionally is the one bug class this repo refuses to import.
- Secrets follow the house pattern: Secrets Manager names in config, never
  values (see `identity:` handling in `platform.yaml` for the precedent).
- No credentials, account ids, or internal endpoints anywhere in the folder.
- The walkthrough is written for a workshop participant: enable → deploy →
  verify, with the failure modes you actually hit while building it.

## Submitting

One branch, one PR against `main`. CI must be green — it already covers your
folder: ruff, the synth parity gate, and ASH. Add your use case to a fixture
under `tests/` if it has pure-Python logic worth pinning. The PR description
says what you verified live and what you could not.
