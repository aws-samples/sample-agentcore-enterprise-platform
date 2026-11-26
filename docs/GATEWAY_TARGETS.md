# Module 7 — Adding a Gateway Target

This is the module where the platform stops being something you deployed and
starts being something you extend. You add a tool to the gateway; every agent
picks it up on its next discovery, **with no agent redeploy**.

Two kinds of target, and the choice is usually obvious:

| | Built-in connector | Lambda target | OpenAPI target |
|---|---|---|---|
| You write | nothing | a Lambda handler + tool schema | an OpenAPI spec |
| Good for | capabilities AWS already operates (web search) | your APIs, your data, your business logic | an existing HTTP API |
| Credentials | none — the gateway's IAM role | whatever your Lambda needs | an AgentCore Identity credential provider (API key or OAuth) |
| Example in this repo | `web-search` (`stacks/gateway_stack.py`) | `sample-tool` (`tools/sample_tool/`) | `tests/fixtures/openapi/sample.json` |

Both are declared in the gateway stack and appear to agents as ordinary MCP
tools. Start by seeing what is registered today:

```bash
python scripts/invoke.py --tools
# sample-tool___text_analysis_tool, web-search___WebSearch
```

The `target___tool` naming matters later — Cedar policies and agent prompts
both refer to tools by that full name.

---

## Path A — a built-in connector (no code)

AWS operates the backend; you register it. The web-search connector already in
this repo is the worked example:

```python
# stacks/gateway_stack.py
web_search = agentcore.CfnGatewayTarget(
    self, "Target-web-search",
    name="web-search",
    gateway_identifier=self._gateway.attr_gateway_identifier,
    credential_provider_configurations=[
        {"credentialProviderType": "GATEWAY_IAM_ROLE"},
    ],
    target_configuration={"mcp": {}},
)
web_search.add_property_override(
    "TargetConfiguration.Mcp.Connector",
    {
        "Source": {"ConnectorId": "web-search"},
        "Configurations": [{"Name": "WebSearch", "ParameterValues": {}}],
    },
)
```

Three details that are easy to get wrong:

1. **The connector config goes through `add_property_override`.** The L1
   construct's property mapping predates connector targets and silently drops
   the key if you pass it in `target_configuration` — the target then deploys
   with no connector and the tool never appears. A test
   (`tests/test_web_search_target.py`) guards this.
2. **The gateway role needs the connector's own action.** For web search that is
   `bedrock-agentcore:InvokeWebSearch` on the service-owned ARN
   `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1` — note the
   literal `aws` where an account id would normally be. Without it the target
   deploys and every call fails at invoke time.
3. **Connectors are regional.** Web search exists in a subset of regions, so
   `app.py` gates it (`WEB_SEARCH_REGIONS`) and turns it off elsewhere rather
   than failing the deploy. Do the same for any connector you add.

Deploy and verify:

```bash
./scripts/deploy.sh deploy --module 7
python scripts/test_gateway.py           # tools/list + one tools/call
```

---

## Path B — a Lambda target (your own logic)

Use this for anything of your own: an internal API, a database query, a
calculation. Three pieces.

### 1. The handler

Copy `tools/sample_tool/handler.py` as the shape to follow. The part that
surprises people is the tool name arrives in the **context**, not the event:

```python
def handler(event, context):
    # The gateway passes "<target>___<tool>"; split on the delimiter and
    # dispatch. One Lambda can serve several tools.
    delimiter = "___"
    tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = tool_name[tool_name.index(delimiter) + len(delimiter):]

    if tool_name == "my_tool":
        result = do_the_thing(event.get("some_arg", ""))
        return {"content": [{"type": "text", "text": result}]}
    return {"error": f"Unsupported tool: {tool_name}"}
```

Return shape: `{"content": [{"type": "text", "text": ...}]}` on success,
`{"error": "..."}` on failure. Arguments arrive as top-level keys in `event`.

### 2. The schema

Put the tool schema in `tools/my_tool/tool_spec.json` and declare the target
in `platform.yaml`. `app.py`'s `DEFAULT_TOOL_CONFIGS` (in
`stacks/gateway_stack.py`) is only the default used when `gateway.tools` is
absent; once you list targets, list `sample-tool` too if you still want it:

```yaml
gateway:
  tools:
    - sample-tool                 # the built-in default
    - my-tool                     # tools/my_tool/ + tools/my_tool/tool_spec.json
    - name: other-tool            # explicit form
      type: lambda
      source_dir: tools/other_tool
      env_vars: {LOG_LEVEL: info}
```

`tool_spec.json` is a JSON list in the **PascalCase** CloudFormation shape,
not the MCP JSON. This is the Python equivalent of what
`DEFAULT_TOOL_CONFIGS` holds:

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
                "Properties": {
                    "some_arg": {"Type": "string", "Description": "…"},
                },
                "Required": ["some_arg"],
            },
        },
    ],
},
```

The gateway stack does the rest: it creates the Lambda from `source_dir`, grants
the gateway permission to invoke it, and registers the target.

Write the `Description` for a model, not for a human skimming a table. It is the
only thing the agent has when deciding whether this tool answers the question.

### 3. Deploy and verify

```bash
./scripts/deploy.sh deploy --module 7
python scripts/test_gateway.py
python scripts/invoke.py --tools               # your tool should be listed
```

Then confirm an agent will actually *use* it. Remember the default
`orchestrator` pattern has no tools at all — use a tool-consuming pattern:

```bash
AGENT_PATTERN=strands-agent ./scripts/deploy.sh deploy --module 6
python scripts/invoke.py "Use my_tool on '…' and report what it returns."
```

---

## Path C — an OpenAPI target (an existing HTTP API)

Point the gateway at an OpenAPI spec and an existing AgentCore Identity
credential provider; each operation becomes a tool.

```yaml
gateway:
  tools:
    - sample-tool
    - name: weather-api
      type: openapi
      schema_path: specs/weather.json      # JSON or YAML, read at synth time
      credential_provider:
        type: api_key                      # or oauth
        arn: arn:aws:bedrock-agentcore:us-east-1:111122223333:token-vault/default/apikeycredentialprovider/weather-key
        # scopes: [read]                   # oauth only
```

Validation (`python -m infra_utils.platform_config platform.yaml`) rejects an
unknown `type`, a missing `schema_path` file, a missing or malformed provider
ARN (and an ARN whose kind does not match `type`), `scopes` on an API key, and
duplicate target names, all in one pass. The spec is inlined as the target's
`OpenApiSchema.InlinePayload`. The gateway role gets
`bedrock-agentcore:GetWorkloadAccessToken` plus
`GetResourceApiKey`/`GetResourceOauth2Token` on the declared provider ARNs and
their vaults only. It also gets `secretsmanager:GetSecretValue` on exactly
the secrets backing those providers. AgentCore Identity names them
`bedrock-agentcore-identity!<vault>/apikey|oauth2/<name>-??????`. Without
that access the target deploys but every call fails at credential retrieval.
Confirm the action names and the secret naming against the current AgentCore
Identity API before relying on them. A provider secret encrypted with a
customer-managed KMS key also needs `kms:Decrypt` on that key.

Caveats:

- **Cedar**: `control-library/cedar/gateway-default` only permits read tools,
  so new OpenAPI tools are denied under `cedar_mode=ENFORCE` until you add
  permits for them.
- **SCPs**: `targets-restrict-type` and `targets-restrict-credential-provider`
  (`control-library/scp/gateway/`) may block OpenAPI targets in governed orgs.
- **Federated**: the gateway deploys only in the platform account, so
  `schema_path` must resolve where that deploy runs.

---

## Governing what you added

A new tool is a new capability reaching outward, so it inherits the gateway's
controls (all opt-in, see [`SECURITY_CONTROLS.md`](SECURITY_CONTROLS.md)):

- **Cedar policies** name tools as `<TargetName>___<tool_name>` — the same
  string `--tools` prints. Add a permit for the new tool, or it is denied once
  `cedar_mode=ENFORCE`.
- **The egress interceptor** applies a Bedrock Guardrail to gateway
  request/response payloads, so it masks PII in your tool's output too.
- **Gateway configuration SCPs** constrain what target types admins may create
  at all (`control-library/scp/gateway/`).

---

## When it does not work

| Symptom | Likely cause |
|---|---|
| Tool absent from `tools/list` | connector set via `target_configuration` instead of `add_property_override`; or a regional connector in an unsupported region |
| Tool listed, calls fail | gateway role missing the connector action, or Lambda invoke permission |
| `Unsupported tool: …` from your Lambda | handler dispatching on the full `target___tool` name instead of the suffix |
| Agent never calls the tool | `Description` too vague, or you are on the `orchestrator` pattern (no tools) |

More in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#the-agent-reports-no-tools-or-only-the-code-interpreter).
