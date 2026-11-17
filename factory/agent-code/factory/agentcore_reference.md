# AgentCore and CloudFormation reference for reviewers

Compact platform facts a reviewer needs to judge a change against. Each section is retrieved on its own,
so each stands alone. Facts are behaviours of the AWS control planes; check the change against them.

## AgentCore Runtime protocol: HTTP vs A2A vs MCP

`protocolConfiguration.serverProtocol` on the runtime (CDK `CfnRuntime` / L2 `Runtime`, property
`protocol_configuration`) decides which contract the container must serve. The container and the
runtime setting must agree; a mismatch deploys fine and fails only at invoke time.
- HTTP: port 8080, `POST /invocations` (JSON body in, JSON or SSE out) and `GET /ping` health check.
  The Python SDK's `BedrockAgentCoreApp` with `@app.entrypoint` serves this contract.
- A2A: port 9000, JSON-RPC 2.0 at `/` (methods such as `message/send`), agent card at
  `/.well-known/agent-card.json`, plus `/ping`. Strands serves it with `A2AServer(agent=...).serve()` /
  `serve_a2a`; a `BedrockAgentCoreApp` entrypoint is NOT an A2A server.
- MCP: port 8000, streamable HTTP at `/mcp`.
A runtime declared A2A (for example an agent other agents call as a peer) whose code only defines an
`@app.entrypoint` handler serves the wrong protocol. Callers of an A2A runtime send JSON-RPC, not a
free-form `{"prompt": ...}` payload.

## AgentCore Runtime inbound auth and request header allowlist

- Inbound auth is either IAM (SigV4, the default) or `authorizerConfiguration.customJWTAuthorizer`
  (`discoveryUrl`, `allowedAudience`, `allowedClients`, optional `allowedScopes`). A runtime has one mode.
- `requestHeaderConfiguration.requestHeaderAllowlist` forwards listed headers to the container. Custom
  headers must start with `X-Amzn-Bedrock-AgentCore-Runtime-Custom-`. `Authorization` may be listed ONLY
  when the runtime has a `customJWTAuthorizer`; with IAM auth the control plane rejects the create/update
  (ValidationException) and the stack rolls back. Code that adds `Authorization` to the allowlist must do
  it conditionally on the JWT authorizer being configured, never unconditionally.
- With IAM auth the caller identity reaches the container only via SigV4 context; with JWT auth the
  container should validate or at least read claims from the forwarded token, not trust payload fields.
- `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` lets an IAM caller assert a user id; it needs the
  `bedrock-agentcore:InvokeAgentRuntimeForUser` permission and is an assertion, not verified identity.

## AgentCore Runtime container and configuration

- Images must be `linux/arm64`. An x86-only base image or a build without `--platform linux/arm64`
  fails at runtime start.
- Run as a non-root user; HOME must be writable (set `HOME=/tmp` or create the user's home).
- Environment variables are set on the runtime (`environmentVariables`); secrets belong in Secrets
  Manager or AgentCore Identity, never in environment variables or the image.
- The execution role needs `bedrock:InvokeModel*` on the exact model / inference-profile ARNs, ECR pull,
  CloudWatch Logs and X-Ray. Session microVMs are reused per `runtimeSessionId` (minimum 33 chars).
- A runtime version is immutable; an endpoint (`DEFAULT` or named) points at a version. Updating
  configuration creates a new version.

## AgentCore Gateway auth, targets and tokens

- Inbound authorizer: `CUSTOM_JWT` (discovery URL, allowed audience/clients/scopes) or `AWS_IAM`.
  `allowedScopes` rejects app-only (client-credentials) tokens that carry `roles` instead of `scp`.
- Outbound auth per target: IAM role (for Lambda / Smithy targets), API key or OAuth2 through an
  AgentCore Identity credential provider. The gateway calls public endpoints only (no VPC targets).
- Tool names are exposed as `<TargetName>___<operationId>`. Renaming a target renames every tool.
- An agent fetching a gateway token with AgentCore Identity (`requires_access_token`, M2M flow) needs a
  workload identity and `GetResourceOauth2Token`; the Identity provider and the client must be in the
  same Region. Tokens must not be logged or returned to the caller.
- AgentCore Policy (Cedar) on a gateway is default-deny once ENFORCE/ACTIVE; there are no wildcard
  actions, so group tools by target.

## CloudFormation exports and list-imports

- `aws cloudformation list-imports --export-name X` fails with ValidationError
  `Export 'X' is not imported by any stack` (exit code 254/255) when the export exists but has no
  consumers. It is not an empty list. Shell code under `set -e` (or a `$(...)` whose status is checked)
  must treat that specific error as "no importers" and still fail on other errors; otherwise a
  deploy/handoff step aborts on the first deploy where nothing imports the export yet.
- `list-exports` and `list-imports` paginate (`NextToken`); the CLI pages automatically, SDK callers must use a paginator.
- Deadly embrace: removing a `Fn::ImportValue` consumer and its export in one deploy fails with
  `Export ... cannot be deleted as it is in use by ...`. Remove the consumer first (or weaken the
  reference), deploy, then remove the export.
- Renaming a CDK construct ID changes the logical ID and replaces the resource (data loss for tables,
  buckets, log groups with retention).

## CloudFormation custom resources

- The provider must PUT a response to the presigned HTTPS `ResponseURL` with `Status`, `PhysicalResourceId`,
  `StackId`, `RequestId`, `LogicalResourceId`. No response leaves the stack waiting up to an hour.
- Changing `PhysicalResourceId` on Update makes CloudFormation send Delete for the old id afterwards.
- Delete must succeed for resources that never got created (Create failed); otherwise rollback hangs.

## IAM policy simulation and the model allow-list

- `iam simulate-principal-policy` evaluates only the action and resource ARNs given. Simulating a
  generic or wildcard model ARN against a policy that allows specific ARNs returns `implicitDeny`, a false
  alarm; simulate the exact ARNs the code will call.
- Invoking through a cross-Region inference profile (`us.` / `global.` prefixes) needs permission on the
  inference-profile ARN (`arn:aws:bedrock:<region>:<acct>:inference-profile/<id>`) AND on the underlying
  foundation-model ARNs (`arn:aws:bedrock:*::foundation-model/<model>`) in every destination Region.
  A guardrail or allow-list check must use the same ARN shapes.
- `iam:PassRole` needs `iam:PassedToService` scoping; `Resource: "*"` on write actions is a finding.

## CodeBuild, shell and deploy scripts

- buildspec `commands` run through a shell (`/bin/sh` by default, bash when `shell: bash`). Unquoted
  parentheses, `&`, `;`, `|`, globs or `$` in an argument are shell syntax: `echo Deploy (prod)` is a
  syntax error. Quote such arguments.
- `set -euo pipefail`: `local x=$(cmd)` masks `cmd`'s exit status; `cmd || true` hides real failures;
  `grep` with no match exits 1 and aborts under `set -e`.
- AWS CLI v2 exit codes: 254 = service error (ValidationError, AccessDenied), 255 = other failures.
  Distinguishing "not found / no importers" from real errors requires matching the error text.
- Scripts that read a stack output must handle the stack or output not existing yet (first deploy).
