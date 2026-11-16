# Change models, teams and settings

Changes after go-live for [Claude Code and Cowork on Amazon Bedrock](../README.md).
None of them interrupts users.

## How every change is applied

Most changes are an edit to `platform.yaml` under `use_cases: claude-3p-on-bedrock:`,
then, from the repository root, signed in to AWS:

```bash
./scripts/deploy.sh design
NON_INTERACTIVE=1 ./scripts/deploy.sh deploy --stack agentcore-workshop-dev-uc-claude-3p-on-bedrock
```

`design` catches typos and models that aren't in the catalog before anything
changes. The deploy takes about 4 minutes and replaces the gateway tasks one
by one. Users see the change after restarting Claude Desktop or Claude Code,
or within an hour without a restart. Replace `agentcore-workshop-dev` if you
changed `project` or `environment`.

## Add a model

1. Find the model's ID in your Region. Prefer `eu.`, `us.` or `apac.` to keep
   requests in that area; `global.` can use any Region worldwide.
   ```bash
   aws bedrock list-inference-profiles --query 'inferenceProfileSummaries[?contains(inferenceProfileId,`anthropic`)].inferenceProfileId' --output table
   ```
2. Add it to the catalog under `models:`:
   ```yaml
       models:
         - { id: claude-sonnet-5-5, label: Claude Sonnet 5.5, bedrock: eu.anthropic.claude-sonnet-5-5 }
   ```
3. Give it to the teams that should use it. A team's first model is the one
   new sessions start on:
   ```yaml
       teams:
         - name: engineering
           entra_roles: [Claude.Engineering]
           models: [claude-sonnet-5-5, claude-opus-5]
   ```
4. Deploy. The gateway's permission to call Bedrock is extended to the new
   model automatically, and only to it.

If a service control policy restricts Regions in your organization, check the
new model's Regions first ([Prepare the AWS account](walkthrough.md#prepare-the-aws-account), step 3).

## Remove a model

Remove its `id` from every team and from `default:`, then remove its line
under `models:`, and deploy. `design` tells you if a team still lists it.

## Let only one team use a model

List the model only in that team's `models:`. The gateway refuses it for
everyone else, also if they type the model name themselves.

## Add a team

1. Entra administrator: create the App Role and assign people to it
   ([Register the Entra application](walkthrough.md#register-the-entra-application), steps 5 and 6).
2. Add the team with the role's exact value, and deploy:
   ```yaml
       teams:
         - name: data-science
           entra_roles: [Claude.DataScience]
           models: [claude-opus-5, claude-sonnet-5-5]
   ```

Teams are checked top to bottom and the first match wins, so put the more
restricted team first if a person can be in both.

## Block a tool for a team

Add the Claude Code tool names to the team's `deny_tools:`. Rules in
`default:` apply to every team.

```yaml
      - name: contractors
        deny_tools: [WebFetch, WebSearch]
```

These rules are applied by Claude Code on the laptop. The model list is
enforced by the gateway.

## Move a person to another team, or remove access

In Entra only: change or remove their App Role assignment, or disable the
user. It takes effect at their next session refresh, at most
`session_ttl_hours` later (default 1 hour). No deploy.

## Show each model only once in Claude Desktop

Models that support a 1M-token context (Opus 4.6 and later, Sonnet 4.6 and
later) appear twice in Claude Desktop: standard and 1M. They're the same
model; the 1M option accepts much larger inputs and costs more for long
requests. To offer only the standard option, add this to `default:` and deploy:

```yaml
    default:
      settings:
        env:
          CLAUDE_CODE_DISABLE_1M_CONTEXT: "1"
```

## Give Claude Code and Cowork the AgentCore tools

The platform's AgentCore Gateway (web search, and any targets you add) shows
up as a server named **agentcore** in Claude Code and in Cowork's
**Connectors**. Each person connects it once with their Entra sign-in.

1. Do [Register the AgentCore Gateway in Entra](walkthrough.md#register-the-agentcore-gateway-in-entra)
   and deploy the platform's `auth` and `gateway` stacks with web search on:
   ```yaml
   gateway:
     web_search: on
   ```
2. Add the block, then deploy as above:
   ```yaml
   use_cases:
     claude-3p-on-bedrock:
       agentcore_tools:
         client_id: "<AgentCore Gateway app id>"   # = identity.client_id
         teams: [engineering]                       # optional; default: everyone
   ```
3. Run `verify.py`. It ends with `AgentCore Gateway sign-in points to Entra`.

To take the tools away from everyone, delete the block and deploy. To limit
them to some teams, list the teams. The gateway URL comes from the platform at
deploy time, so a recreated AgentCore Gateway only needs its new URL added in
Entra ([step 7](walkthrough.md#register-the-agentcore-gateway-in-entra)) and one deploy.

## Add tools to the AgentCore Gateway

Tools are targets on the platform's AgentCore Gateway, not part of this use
case. Add one as described in [Gateway targets](../../../docs/GATEWAY_TARGETS.md)
and deploy the `gateway` stack. People see the new tools the next time Claude
Code or Cowork lists them, with no change here.

## Upgrade Claude Code on the gateway

Change `claude_version` and deploy. This is the only change that rebuilds the
container image (about 5 extra minutes): the new release is downloaded and its
signature checked first. To see the current stable version:

```bash
curl -fsSL https://downloads.claude.ai/claude-code-releases/stable
```

## Rotate the Entra client secret

Before the old secret expires:

1. Entra administrator: create a **new** client secret. Keep the old one for now.
2. Cloud team: store the new value (hidden input) and restart the gateway so
   it reads it:
   ```bash
   umask 077; read -rs S; printf '%s' "$S" > /tmp/s.txt; unset S
   aws secretsmanager put-secret-value --secret-id claude-3p/entra-oidc --secret-string file:///tmp/s.txt; rm -f /tmp/s.txt
   ```
   Then restart the gateway tasks: ECS console → the `uc-claude-3p-on-bedrock`
   cluster → the service → **Update service → Force new deployment**.
3. Once sign-in works, delete the old secret in Entra.

The wizard can also replace the value: run it and answer **yes** at the
secret step.

## Set spend limits

Spend limits per person or team are set live through the gateway's admin API,
not in `platform.yaml`. People with an App Role in `spend_admin_roles` can
manage them. See Anthropic's [spend limits](https://code.claude.com/docs/en/claude-apps-gateway-spend-limits).
