# Migration adapter

Runs an **unmodified customer agent container** on AgentCore Runtime. The
runtime's contract is fixed — arm64, HTTP on port 8080, `POST /invocations`,
`GET /ping` — and an existing agent speaks its own HTTP. The adapter is a thin
layer built `FROM` the customer's image: it serves the AgentCore contract,
starts the original command as a child process in the same container, and
forwards each invocation to it over localhost. The customer's code does not
change.

```
AgentCore Runtime ──POST /invocations──▶ adapter (:8080) ──POST──▶ child (:MIGRATION_PORT + MIGRATION_INVOKE_PATH)
```

This directory is **not an agent pattern** (don't set `AGENT_PATTERN=adapter`).
It is selected by the `migration:` block in `platform.yaml` and built by the
migration buildspec in `stacks/runtime_stack.py`.

## Environment contract

Set on the runtime by `stacks/runtime_stack.py` (values come from
`platform.yaml`'s `migration:` block):

| Variable | Meaning | Default |
|---|---|---|
| `MIGRATION_PORT` | Port the child listens on | `8000` |
| `MIGRATION_INVOKE_PATH` | Child endpoint that receives the forwarded payload | `/invocations` |
| `MIGRATION_HEALTH_PATH` | Child health endpoint (2xx/3xx = up). Empty = no declared health route: any HTTP response on `/` counts as up | empty |
| `MIGRATION_ENV` | Extra child env, `KEY=VALUE,KEY2=VALUE2` | empty |
| `MIGRATION_SECRETS` | Comma-separated ENV NAMES resolved from Secrets Manager into the **child's** env only. Secret id: `<PROJECT_NAME>/<ENVIRONMENT>/migration/<NAME>` | empty |
| `MIGRATION_SECRET_ARNS` | Optional per-name secret-id override, `NAME=arn,NAME2=arn2` | empty |
| `MIGRATION_STARTUP_TIMEOUT` | Seconds to wait for child health before exiting non-zero | `60` |
| `ADAPTER_CHILD_CMD` | JSON array — the source image's Entrypoint+Cmd. **Baked at image build**, not set at deploy time | — |
| `PROJECT_NAME` / `ENVIRONMENT` | Used for the default secret naming | — |

Behavioural notes:

- **Startup**: secrets are resolved first (fail fast, naming the missing
  secret), then the child starts, then the adapter waits up to
  `MIGRATION_STARTUP_TIMEOUT` for the health URL; on timeout it exits non-zero
  printing the child's last stderr lines.
- **Response mapping**: JSON reply → returned as-is; plain text →
  `{"status":"success","response": <text>}`; non-2xx →
  `{"status":"error","code":…,"error":…}`; `text/event-stream` → re-streamed
  as SSE events.
- **Supervision**: a dead child is restarted once; after a second death every
  invocation returns a structured 503-style error.
- **`GET /ping`** is BedrockAgentCoreApp's default handler — it reflects
  *adapter* liveness, not the child's (an arbitrary customer container has no
  universal health hook).
- **User**: installing python3 + the adapter deps requires root, so the child
  runs as root even if the source image dropped privileges. Known deviation.

## The arm64 constraint

AgentCore Runtime only runs `linux/arm64` images. The buildspec pulls the
source image with `--platform linux/arm64` and fails with a clear message when
no arm64 variant exists. For amd64-only images, either rebuild from source
(`migration.source.build.context` in `platform.yaml`) or use the `ec2` target.

## Local test with finch (no AWS credentials needed)

```bash
finch vm start

# 1. Build the stand-in customer agent (arm64, like the real pipeline)
cd workshop-simulation/existing-ec2-agent
finch build --platform linux/arm64 -t standin:dev .

# 2. Build the adapter on top of it
cd ../migration-adapter
finch build --platform linux/arm64 \
  --build-arg SOURCE_IMAGE=standin:dev \
  --build-arg CHILD_CMD='["python","server.py"]' \
  -t adapter:dev .

# 3. Run without credentials — forwarding works; only the Bedrock call fails
finch run -d --name adapter-test \
  -e MIGRATION_PORT=8000 -e MIGRATION_INVOKE_PATH=/run \
  -e MIGRATION_HEALTH_PATH=/healthz -e MIGRATION_SECRETS= \
  adapter:dev

# 4. Check from INSIDE the container (localhost ports on the host may be
#    hijacked by local proxies — docs/TESTING.md):
finch exec adapter-test python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=5).read())"
finch exec adapter-test python3 -c "import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/invocations', data=json.dumps({'prompt':'hi'}).encode(), headers={'Content-Type':'application/json'})); print(r.read().decode())"
# expect: {"status":"error", ...credentials...} — forwarding proved, Bedrock (rightly) unreachable

finch rm -f adapter-test
```
