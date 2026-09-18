# Kiro power for this accelerator

[`agentcore-enterprise-platform/`](agentcore-enterprise-platform) is a
[Kiro](https://kiro.dev) **power**: operational knowledge about *this* repository,
packaged so an agent can drive it — pick a deployment profile, deploy and verify a
module, diagnose a failure, audit what is billing, tear it down.

It carries what a checkout does not: which profile fits a given situation, what
each module actually deploys versus what its title suggests, which command proves
a layer works, why module 6 goes silent for seven minutes, which flags are sharp,
and which security controls are enforced rather than merely declared.

## Install it

Powers are added through the Kiro UI; there is no CLI for it.

**Powers panel → Add Custom Power → Local Directory**, then paste the absolute
path of the `agentcore-enterprise-platform/` directory inside your clone — not the
repository root, and not this `kiro/` directory:

```bash
echo "$(git rev-parse --show-toplevel)/kiro/agentcore-enterprise-platform"
```

After pulling a change to these files: **Powers panel → the power → Check for
Updates → Update Power**.

The power bundles two MCP servers (`mcp.json`): the AgentCore MCP server, which
needs [uv](https://docs.astral.sh/uv/) on `PATH` for `uvx`, and the AWS Knowledge
server over HTTP. Nothing in either is pre-approved — Kiro strips `autoApprove`
from a power's `mcp.json` on load, so a power cannot grant itself auto-approval.
Approving the read-only tools and leaving anything that creates, updates, deletes
or invokes on manual confirmation is the posture to aim for.

## Layout, and why it is this exact shape

```
agentcore-enterprise-platform/
  POWER.md          router: profile picker, module map, the sharp edges, steering index
  mcp.json          MCP servers only, no display metadata
  steering/         the detail — loaded on demand, one file per question shape
    runbook-*.md      six procedures, for driving rather than explaining
```

Kiro's power installer copies a fixed allowlist — `POWER.md`, `mcp.json`, and
`.md` files under `steering/` — and its validator **rejects** a power directory
containing anything else that looks like a script, an archive, a credential, or a
**hidden file at any depth**. So `.DS_Store` landing in here makes the power fail
to install with an error that does not mention `.DS_Store`. That is why this
README lives one level up, outside the power directory, and why
`scripts/check-kiro-power.sh` checks installability rather than trusting it.

It is also why the six runbooks are `steering/runbook-*.md` rather than the
`skills/<name>/SKILL.md` layout Kiro's skill *reader* expects: `skills/` is not on
the installer's allowlist, so an installed power has no `skills/` directory and
every runbook read fails. `POWER.md` routes to the filenames that actually exist.

## Changing it

Run the gate before you commit:

```bash
bash scripts/check-kiro-power.sh
```

It runs in CI on every pull request and makes no AWS calls and no network calls.
Beyond the power format, it checks the claims against the tree they are shipped
with: every `file:line` citation resolves and is in bounds, every restatement of a
profile's module sequence matches `PROFILE_MODULES` in `scripts/deploy.sh`, and
every `--flag` cited next to one of this repo's scripts exists in that script.

That coupling is the point of shipping the power here rather than in a repository
of its own. A hallucinated flag reads exactly like a real one and only fails in
front of a user, and a renamed flag is indistinguishable from a hallucinated one
a month later. Keeping these files next to the code means a rename breaks the
build instead of quietly breaking someone's session.

**The one rule that matters most: every claim must be verifiable in this
repository's source.** Prefer a real run over a reading — the timings and error
strings in `steering/troubleshooting.md` came from actual deployments. If you
cannot confirm something, cut it rather than hedging it.
