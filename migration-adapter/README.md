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
| `MIGRATION_ENV_JSON` | Extra child env as a JSON object whose values must all be strings. Preferred and lossless for commas/equals | `{}` |
| `MIGRATION_ENV` | Legacy extra child env, `KEY=VALUE,KEY2=VALUE2`. Used only when `MIGRATION_ENV_JSON` is absent; values cannot contain commas | empty |
| `MIGRATION_SECRETS` | Comma-separated ENV NAMES resolved from Secrets Manager into the **child's** env only. Secret id: `<PROJECT_NAME>/<ENVIRONMENT>/migration/<NAME>` | empty |
| `MIGRATION_SECRET_ARNS` | Optional per-name secret-id override, `NAME=arn,NAME2=arn2` | empty |
| `MIGRATION_STARTUP_TIMEOUT` | Seconds to wait for child health before exiting non-zero | `60` |
| `MIGRATION_LIVE_HEALTH_TIMEOUT` | Maximum duration of each pre-invocation child health probe; values are capped at 5 seconds | `2` |
| `ADAPTER_CHILD_CMD` | JSON array — the source image's Entrypoint+Cmd. **Baked at image build**, not set at deploy time | — |
| `ADAPTER_CHILD_USER` | Source image's `Config.User`, copied from the `CHILD_USER` build argument. **Baked at image build**, not set at deploy time | `root` |
| `PROJECT_NAME` / `ENVIRONMENT` | Used for the default secret naming | — |

Behavioural notes:

- **Child environment**: when `MIGRATION_ENV_JSON` is present it is
  authoritative, even if the legacy variable is also present. Invalid JSON,
  non-object JSON, or any non-string value fails startup before the child is
  launched. For example, `{"OPTIONS":"a=b,c=d"}` arrives unchanged.
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
- **Live health**: every invocation first confirms that the child process is
  alive and performs one bounded GET to `MIGRATION_HEALTH_PATH`. A declared
  route must return 2xx/3xx. With no declared route, any response from `/`
  proves the endpoint is accepting connections. An unhealthy process or
  endpoint returns a structured 503-style error and the payload is not sent.
- **`GET /ping`** is BedrockAgentCoreApp's default handler — it reflects
  *adapter* liveness, not the child's. The installed SDK is not extended with
  an undocumented route override; child health is enforced at `/invocations`.
- **User preservation**: installation runs as root, then Docker switches the
  adapter back to the source image's configured user. The child inherits the
  adapter uid. Startup fails closed if a non-root `ADAPTER_CHILD_USER` was
  declared but the container is running as root.

## Parent build contract

The migration image builder must inspect the source image and pass all three
arguments:

```bash
SOURCE_USER="$(docker image inspect --format '{{.Config.User}}' "$SOURCE_IMAGE")"
[ -n "$SOURCE_USER" ] || SOURCE_USER=root

docker build --platform linux/arm64 \
  --build-arg SOURCE_IMAGE="$SOURCE_IMAGE" \
  --build-arg CHILD_CMD="$SOURCE_COMMAND_JSON" \
  --build-arg CHILD_USER="$SOURCE_USER" \
  -f adapter/Dockerfile adapter/
```

`CHILD_USER` accepts Docker's user forms (`name`, `uid`, `name:group`, or
`uid:gid`). The Dockerfile bakes it into `ADAPTER_CHILD_USER` and ends with
`USER ${CHILD_USER}`. The parent must normalize an empty source `Config.User`
to `root`; it must not infer a non-root user.

## The arm64 constraint

AgentCore Runtime only runs `linux/arm64` images. The buildspec pulls the
source image with `--platform linux/arm64` and fails with a clear message when
no arm64 variant exists. For amd64-only images, supply the Docker build context
through `migration.source.build.context` so CodeBuild can produce arm64.

## Local test with finch (no AWS credentials needed)

```bash
finch vm start

# 1. Build the stand-in customer agent (arm64, like the real pipeline)
cd workshop-simulation/existing-ec2-agent
finch build --platform linux/arm64 -t standin:dev .

# 2. Build the adapter on top of it
cd ../../migration-adapter
finch build --platform linux/arm64 \
  --build-arg SOURCE_IMAGE=standin:dev \
  --build-arg CHILD_CMD='["python","server.py"]' \
  --build-arg CHILD_USER=10001:10001 \
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
