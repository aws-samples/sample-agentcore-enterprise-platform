# Agent patterns and protocols

Read this when choosing or swapping an agent framework, wiring A2A or AG-UI, or
adding a tool to the gateway.

**The platform is framework-agnostic: the agent pattern is a config value, not a
rewrite.** That is the claim the whole pitch rests on, and it is the single most
effective thing to demonstrate live.

```bash
AGENT_PATTERN=langgraph-agent ./scripts/deploy.sh deploy --module 6
```

One redeploy of the orchestrator runtime, no infrastructure change. A bad pattern
name is rejected **up front**, before any AWS call, rather than failing later inside
CodeBuild. Measured on a warm account: **172s** for `orchestrator` → `langgraph-agent`,
including the CodeBuild rebuild — quicker than module 6's first deploy because only
the image changes. Confirm the swap really happened rather than trusting the exit code:

```bash
ARN=$(aws ssm get-parameter --name /$PROJECT_NAME/$ENVIRONMENT/runtimes/orchestrator/arn \
  --query Parameter.Value --output text)
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${ARN##*/}" \
  --query '[agentRuntimeVersion,protocolConfiguration,networkConfiguration.networkMode]'
aws ecr describe-images --repository-name $PREFIX-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-2:].imageTags'
```

A bumped `agentRuntimeVersion` **and** a new image tag is the proof. Either alone is not.

### Rehearse the swap before demoing it: the output shape changes

**`invoke.py` prints whatever the pattern emits, and the streaming patterns emit raw
SSE.** This is the single most likely way the framework-swap demo goes wrong, because
it looks like a crash and is not one:

| Pattern | What `invoke.py` prints |
|---|---|
| `orchestrator` | one line — `{"status": "success", "response": "VPC OK"}` |
| `langgraph-agent` | **176 lines** of `data: {"content": [{"type":"text","text":" noted that your favour"}], …}` — one chunk per few tokens |

The answer is in there, spread across the `content[].text` fields, but nobody reads it
off the screen. Two options, both fine — just pick one *before* the session:

- Demo the swap with `--tools` or a stack/ECR diff, and keep the prose answer on the
  `orchestrator` pattern.
- Or pipe it through a reassembler so the room sees a sentence:

```bash
.venv/bin/python scripts/invoke.py "…" \
  | python3 -c 'import sys,json,re
print("".join(p["text"] for l in sys.stdin if l.startswith("data: ")
  for p in (json.loads(l[6:]).get("content") or []) if isinstance(p,dict) and p.get("text")))'
```

Do not "fix" this by switching to `--agui`: that flag is for the `agui-*` patterns and
fails on protocol against an `HTTP` runtime.

---

## The seven patterns

Set with `AGENT_PATTERN=…` or `-c agent_pattern=…`, or `agents.pattern` in
`platform.yaml`.

| Pattern | Protocol | Verifies caller JWT | Gateway tools | Uses Memory | Default model | Notes |
|---|---|---|---|---|---|---|
| `orchestrator` *(default)* | HTTP | no | **none** | **no** | `us.anthropic.claude-sonnet-4-6` | deliberately minimal |
| `strands-agent` | HTTP | yes | not wired in this pattern yet | yes | `us.anthropic.claude-sonnet-4-6` | |
| `langgraph-agent` | HTTP | yes | yes | yes | `us.anthropic.claude-sonnet-4-6` | needs Memory data-plane IAM (`ListEvents`) |
| `claude-sdk-agent` | HTTP | yes | yes | **no** | `us.anthropic.claude-opus-4-6-v1` | replies include `claude_session_id` |
| `claude-sdk-multi-agent` | HTTP | yes | yes | yes | `us.anthropic.claude-opus-4-6-v1` | delegates to a `code-analyst` subagent |
| `agui-strands-agent` | **AGUI** | yes | yes | yes | `us.anthropic.claude-sonnet-4-6` | typed SSE; invoke with `--agui` |
| `agui-langgraph-agent` | **AGUI** | yes | yes | yes | `us.anthropic.claude-sonnet-4-6` | slower first response (graph built per request) |

**"Uses Memory" is not the same as "Memory exists."** A `-memory` stack and a live
Memory resource land at module 6 in every profile, and `MEMORY_ID` is injected into
every runtime's environment — but only the patterns marked yes above actually read
or write it. `agent-code/orchestrator/` contains no reference to memory at all.

Verified consequence: on a clean `greenfield` deploy, with Memory `ACTIVE` and
`test_memory.py` passing 5/5, the default orchestrator still does not remember
across turns of the **same** session:

```bash
.venv/bin/python scripts/invoke.py --session demo-1 "Remember my favourite colour is teal."
#   "I'll remember that your favourite colour is teal!"
.venv/bin/python scripts/invoke.py --session demo-1 "What is my favourite colour?"
#   "I don't have any information about your favourite colour!"
```

The first reply is the model being agreeable, not a write. **Never build a memory
demo on the default pattern** — the recall step fails in front of the room. Check it
against source before promising it:

```bash
grep -rli memory agent-code/<pattern>/
```

### …and a memory-using pattern is still not enough for cross-session recall

Measured on `langgraph-agent`, deployed and healthy, with Memory `ACTIVE`:

| Question | Result |
|---|---|
| Recall within the **same** `--session` | **works** — "Your favourite colour is **teal**! You told me that just a moment ago in this conversation." |
| Recall in a **different** session | **`NO RECORD`** |

Events really are being written — `list-sessions` showed all three session ids under
the actor. What is missing is the strategy that turns them into recallable facts. The
deployed memory has exactly one, and it is not a semantic one:

```bash
aws bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
  --query 'memory.strategies[].{type:type,status:status}'
#   → [{"type": "USER_PREFERENCE", "status": "ACTIVE"}]
```

Semantic fact extraction is added only when `use_long_term_memory=true`, which
defaults to **`false`** (`app.py:142-143`) because it costs more. So:

```bash
USE_LONG_TERM_MEMORY=true AGENT_PATTERN=langgraph-agent \
  ./scripts/deploy.sh deploy --module A --module 6
```

Be careful with the accelerator's own module-A narration, which says agents "keep
context **across sessions**." True with the flag on; not true of what the guided run
deploys by default. Demo within one session, or turn the flag on beforehand.

### The Memory actor is the caller's `sub` — which collapses for M2M callers

The verified `sub` becomes the AgentCore Memory `actor_id`. For a Cognito
`client_credentials` token there is no human subject, so `sub` is the **app client
id** — and `scripts/invoke.py` uses exactly that flow. Measured:

```bash
aws bedrock-agentcore list-actors --memory-id "$MEMORY_ID"
#   → actorId: 39p8o8b978f15rf1932c6lot0g   ← identical to /auth/m2m-client-id
#   → actorId: test-user-12345              ← written by test_memory.py
```

**Every `invoke.py` call in the room shares one actor.** That is fine for a demo and
wrong for any statement about per-user isolation: you cannot show tenant separation
through `invoke.py`, because there is only one tenant in the data. Demonstrating the
boundary needs two real user tokens (`3LO`/federated sign-in), not two `--session`
ids. See `patterns.md` → Tenant isolation.

Plus the two A2A sub-agents, which are not orchestrator patterns:
`code-agent` and `research-agent` (both `A2A`, both default
`us.anthropic.claude-sonnet-4-6`).

### Model override

`MODEL_ID` unset means each pattern falls back to its own in-code
`DEFAULT_MODEL_ID` above. Those are dated ids, and dated ids eventually get
marked Legacy and rejected in fresh accounts — so in a new account, override with
a current cross-region inference profile:

```bash
export MODEL_ID=us.anthropic.claude-sonnet-5
```

`MODEL_ID` applies to **all** agents in the deployment, not per pattern.

### The default has no tools, on purpose

`orchestrator` ships toolless and extracts no caller identity. Asking it "what
tools do you have?" correctly returns nothing — that is not a broken deployment.
Route tool questions to the gateway (`invoke.py --tools`, `test_gateway.py`), or
deploy a tool-consuming pattern.

**Every pattern shares `agent-code/` but builds its own Dockerfile, so a green
deploy of one proves nothing about the others.** Run the matrix before a release
(see the bottom of this file) — it has caught a stale container image, a missing
forwarded header, and two missing runtime-role permissions, none of which fail at
synth or deploy time.

---

## Protocols, and how each is invoked

`infra_utils/runtime_protocol.py` decides the protocol from the pattern, and the
protocol decides both the authorizer and the client call.

| Protocol | Who | Inbound auth | Invoke with |
|---|---|---|---|
| `HTTP` | orchestrator patterns | Bearer JWT (CUSTOM_JWT authorizer) | `invoke.py "…"` |
| `AGUI` | `agui-*` patterns | Bearer JWT | `invoke.py --agui "…"` |
| `MCP` | an MCP-server runtime | Bearer JWT | — |
| `A2A` | `code-agent`, `research-agent` | **SigV4**, IAM `InvokeAgentRuntime` | `invoke.py --a2a <component> "…"` |

A JWT authorizer is attached **only** when the protocol is client-facing (`HTTP`,
`MCP`, `AGUI`) **and** a Cognito issuer was supplied. The `Authorization` header
allowlist is gated on exactly the same condition, because the control plane
enforces it: creating a runtime that allowlists `Authorization` without a
`customJWTAuthorizer` is rejected with a validation error.

Consequence worth internalising: **an A2A runtime never sees an `Authorization`
header at all.** Its agent cannot require a caller JWT; IAM guards it instead.
Sending a bearer token to an A2A runtime is rejected exactly like sending SigV4 to
the orchestrator — `Authorization method mismatch` cuts both ways.

Always prefer `scripts/invoke.py`: it picks the right mechanism per target.

---

## A2A — agent to agent

Enabled by `enable_a2a=true`. `--module 8` exports it automatically because the
sub-agent stacks do not exist in the CDK app otherwise.

Sub-agents speak **JSON-RPC 2.0 on `0.0.0.0:9000`** with a `message/send`
envelope. The contract, served by `agent-code/shared/a2a_serve.py`:

| Endpoint | Method | Returns |
|---|---|---|
| `/` | POST | JSON-RPC |
| `/.well-known/agent-card.json` | GET | the agent card |
| `/ping` | GET | `{"status": "Healthy"}` |

**A sub-agent that returns HTTP 424 is serving the wrong protocol — it is not a
payload problem**, whatever the repo's `docs/TROUBLESHOOTING.md` says. Measured:
sending the plain `{"prompt": ...}` shape to a working `code-agent` returns **HTTP
200** with a JSON-RPC `-32600 "Request payload validation error"` body, not a 424.
424 means the image serves HTTP `/invocations` on 8080 while the runtime is
registered `A2A` — check all three endpoints on port 9000 and rebuild.

```bash
.venv/bin/python scripts/invoke.py --a2a code-agent "Reply with exactly: A2A OK"
.venv/bin/python scripts/invoke.py --a2a research-agent "…"
```

Why this design rather than one big agent: specialized agents get **independent
auth, scaling, and lifecycle**. That is the architectural argument to make in a
session — the code-agent can be rate-limited, redeployed, or IAM-restricted
without touching the orchestrator.

---

## AG-UI

The `agui-*` patterns speak the AG-UI protocol — typed SSE events, intended for
building a UI on top of the agent rather than consuming a single response body.
They report protocol `AGUI` and **must** be invoked with `--agui`; calling them
without it fails on the protocol, not on auth.

`agui-langgraph-agent` has a noticeably slower first response because it builds
its graph per request. Expected.

---

## Caller identity

`agent-code/shared/auth.py` (`extract_user_id_from_context`) verifies the token
signature against the issuer's published JWKS, pinned to `RS256`, then
`agent-code/shared/jwt_claims.py` checks `iss`, the client, and the presence of
`sub`. **Identity is the `sub` claim.** Every failure path raises — there is no
fallback to an unverified decode.

| Condition | Result |
|---|---|
| No `Authorization` header | reject |
| `COGNITO_ISSUER_URL` unset | reject, *before* decoding |
| Bad signature, expired, unknown `kid`, JWKS unreachable | reject |
| `iss` mismatch | reject |
| Client not in `COGNITO_ALLOWED_CLIENTS` | reject |
| `COGNITO_ALLOWED_CLIENTS` **empty** | **accepted** — issuer-only pinning |
| No `sub` | reject |

The asymmetry in the last two rows matters: a missing issuer is a hard reject,
but a missing client allowlist degrades quietly to "any client of the correct
issuer." **Set both.** `token_use` is deliberately not checked, so both Cognito
access tokens and ID tokens are accepted as identity.

**Why check twice** — AgentCore Runtime's `CUSTOM_JWT` authorizer already
validated the token before the container saw it. The agent validates it again so
the check travels with your code rather than depending on how the runtime happens
to be deployed. Runtime and Gateway have **independent** authorizers; configuring
one says nothing about the other.

**M2M callers:** Cognito `client_credentials` access tokens carry `client_id`
instead of `aud`. Checking only `aud` would reject machine callers, including
`scripts/invoke.py`. So `jwt_claims.py` prefers `aud` when present (first element
of a list) and falls back to `client_id`, and `auth.py` passes `verify_aud: False`
to validate the audience itself.

**Where the identity goes:** the verified `sub` becomes the AgentCore Memory
`actor_id` — the tenant boundary for stored conversation history. An unverified or
defaulted identity would file different callers under one actor and mix their
history, which is why agents reject rather than substitute a placeholder.

**Injection detail worth knowing when extending:** `COGNITO_ISSUER_URL` and
`COGNITO_ALLOWED_CLIENTS` are injected by **`app.py`**, not by `RuntimeStack`, and
today **only for the orchestrator runtime**. `RuntimeStack`'s own `env_vars` carry
only `PROJECT_NAME`, `ENVIRONMENT`, `COMPONENT_NAME`, `AWS_REGION_NAME`,
`SOURCE_HASH`. The A2A runtimes receive neither variable — harmless only because
their entrypoints take no `RequestContext` and never call the helper. **Any agent
you add there that does call it will fail closed until `app.py` passes the issuer
through.**

JWKS is fetched on the first verified request and cached by `PyJWKClient`, which
refreshes on a 5-minute lifespan and re-fetches on an unknown key id — so Cognito
key rotation is handled. No explicit timeout is set, so a hung JWKS endpoint
stalls the request for PyJWT's 30-second default before failing closed.

An agent that imports `shared/` must also `COPY shared/ shared/` in its
Dockerfile.

---

## Outbound auth — agent to gateway

Separate mechanism, same file. `get_gateway_access_token()` uses:

```python
@requires_access_token(provider_name=os.environ["GATEWAY_CREDENTIAL_PROVIDER_NAME"],
                       auth_flow="M2M")
```

The decorator is evaluated **at import time** and `provider_name` defaults to
`""`, so a runtime without that variable binds an empty provider name at module
load. That is the mechanism behind "silently no tools." The A2A runtimes
intentionally do not receive it.

Flow: runtime → AgentCore Identity `gateway-m2m` credential provider (Token
Vault) → `client_credentials` against Cognito → gateway JWT back to the runtime →
runtime calls the gateway over MCP with that JWT → gateway invokes its Lambda tool
targets.

---

## Adding a tool to the gateway

This is module 7's substance. **Agents pick up new tools on their next discovery,
with no agent redeploy** — worth demonstrating rather than asserting.

Two kinds of target:

| | Built-in connector | Lambda target |
|---|---|---|
| You write | nothing | a handler + tool schema |
| Good for | capabilities AWS operates (web search) | your APIs, data, business logic |
| Credentials | the gateway's IAM role | whatever your Lambda needs |
| Example | `web-search` in `stacks/gateway_stack.py` | `sample-tool` in `tools/sample_tool/` |

### Built-in connector

Registered via `CfnGatewayTarget` with
`credentialProviderConfigurations: [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]`
and `target_configuration={"mcp": {}}`, then the connector itself set through
`add_property_override("TargetConfiguration.Mcp.Connector", {...})`.

Three ways this goes wrong:

1. **Passing the connector in `target_configuration` instead of
   `add_property_override`.** The L1 construct's property mapping predates
   connector targets and **silently drops the key** — the target deploys with no
   connector and the tool never appears. A repo test guards this.
2. **Missing the connector's own IAM action on the gateway role.** Web search
   needs `bedrock-agentcore:InvokeWebSearch` on
   `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1` — note the literal
   `aws` where an account id would normally be. Without it the target deploys and
   every call fails at invoke time.
3. **Forgetting connectors are regional.** `app.py` gates web search on
   `WEB_SEARCH_REGIONS` = `us-east-1`, `eu-west-1`, `ap-northeast-1` and turns it
   off elsewhere rather than failing the deploy. Do the same for anything you add.

### Lambda target

The handler receives the tool name in the **context**, not the event:

```python
def handler(event, context):
    delimiter = "___"
    tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = tool_name[tool_name.index(delimiter) + len(delimiter):]

    if tool_name == "my_tool":
        return {"content": [{"type": "text", "text": do_the_thing(event.get("some_arg", ""))}]}
    return {"error": f"Unsupported tool: {tool_name}"}
```

Arguments arrive as top-level keys in `event`. Return
`{"content": [{"type": "text", "text": ...}]}` on success, `{"error": "..."}` on
failure. One Lambda can serve several tools — dispatch on the suffix after `___`,
not the full name.

Declare the schema in `app.py`'s `tool_configs` beside `sample-tool`, using
**PascalCase** keys — this is the CloudFormation shape, not MCP JSON:

```python
"my-tool": {
    "source_dir": "tools/my_tool",
    "env_vars": {},
    "tool_schema": [
        {
            "Name": "my_tool",
            "Description": "What it does — the agent reads this to decide when to call it.",
            "InputSchema": {
                "Type": "object",
                "Properties": {"some_arg": {"Type": "string", "Description": "…"}},
                "Required": ["some_arg"],
            },
        },
    ],
},
```

The gateway stack does the rest: creates the Lambda from `source_dir`, grants the
gateway permission to invoke it, registers the target.

**Write the `Description` for a model, not for a human skimming a table.** It is
the only thing the agent has when deciding whether this tool answers the
question. "Agent never calls the tool" is usually a vague description — or the
`orchestrator` pattern, which has no tools at all.

### Verify, then prove an agent uses it

```bash
./scripts/deploy.sh deploy --module 7
.venv/bin/python scripts/test_gateway.py                  # tools/list + one tools/call
.venv/bin/python scripts/invoke.py --tools                # your tool should be listed

AGENT_PATTERN=strands-agent ./scripts/deploy.sh deploy --module 6
.venv/bin/python scripts/invoke.py "Use my_tool on '…' and report what it returns."
```

### Tool naming

Tools are `<TargetName>___<tool_name>` — e.g. `sample-tool___text_analysis_tool`,
`web-search___WebSearch`. That full string is what `--tools` prints, what agent
prompts refer to, and what **Cedar policies must name**. A new tool is denied once
`cedar_mode=ENFORCE` unless a permit names it.

---

## The pattern matrix

Run before a release, or when a customer asks "does this really work with our
framework." One orchestrator redeploy per pattern, ~5–8 minutes each:

```bash
export AWS_REGION=us-east-1

for p in orchestrator strands-agent langgraph-agent claude-sdk-agent claude-sdk-multi-agent; do
  AGENT_PATTERN=$p NON_INTERACTIVE=1 ./scripts/deploy.sh deploy \
    --stack $PREFIX-runtime-orchestrator
  .venv/bin/python scripts/invoke.py "Reply with exactly: $p LIVE"
done

for p in agui-strands-agent agui-langgraph-agent; do
  AGENT_PATTERN=$p NON_INTERACTIVE=1 ./scripts/deploy.sh deploy \
    --stack $PREFIX-runtime-orchestrator
  .venv/bin/python scripts/invoke.py --agui "Reply with exactly: $p LIVE"
done
```

Then assert more than "it answered":

```bash
# Right pattern, right protocol, fresh image
ARN=$(aws ssm get-parameter --name /$PROJECT_NAME/$ENVIRONMENT/runtimes/orchestrator/arn \
  --query Parameter.Value --output text)
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${ARN##*/}" \
  --query '[agentRuntimeVersion,protocolConfiguration,agentRuntimeArtifact]'

# Each pattern pushed its own tag — identical tags mean no rebuild happened
aws ecr describe-images --repository-name $PREFIX-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[-5:].{tags:imageTags,pushed:imagePushedAt}'

# Tools actually loaded (exercises the gateway MCP client + token vault)
.venv/bin/python scripts/invoke.py "List the names of the tools you have available. Names only."
# Expect e.g. sample-tool___text_analysis_tool, execute_python_securely
```

Close the loop on observability afterwards — this is what makes module 9's
"end-to-end traces" claim true, reusing a span from the invokes above:

```bash
.venv/bin/python scripts/check_observability.py --spans
```
