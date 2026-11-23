# Asana sync (workstation)

The factory's only path to the Asana board. AWS holds no Asana credential: the agents queue board
changes in `software-factory-asana-ops` and read the board from `software-factory-asana-snapshot`.
This script, on the maintainer's workstation, moves both through the Enterprise Asana MCP under
the maintainer's own Midway session. See `agent-code/factory/asana.py` for the board flow.

| Section | Who moves a task in | What the factory does |
|---|---|---|
| Needs design | a person | the designer writes a spec as a comment, moves the task to Design review |
| Design review | the designer, the PM | nothing: a person reads the spec |
| Ready for agent | a person (this is the approval) | the developer builds it and opens a PR |
| Agent: in progress, In review | the developer, the reviewer | the reviewer comments its review of any PR naming the task |
| Done | the merge handler | the task is completed when its PR merges |
| Needs scoping | the developer, a person | nothing: parked |

The factory acts on a task in Needs design or Ready for agent only when the person who moved it
there is in `asana_users`. The sync refuses any op that moves or creates a task into those two
sections, so the factory can never approve its own work.

Teammates link a PR to its task with a branch named `asana/<task gid>-...` or a line
`Asana-Task: <task gid>` in the PR body. The repo is public, so not the task URL.

## Install

Needs `enterprise-asana-mcp` (`aim mcp install enterprise-asana-mcp`), `isengardcli`, `uv`, and
the `SoftwareFactory` stack deployed with `-c factory_tracker=asana`.

```bash
mkdir -p ~/.config/software-factory ~/.config/systemd/user
echo "FACTORY_ASANA_PROJECT=<project gid>" > ~/.config/software-factory/asana-sync.env
cat > ~/.config/software-factory/aws-config <<CFG
[profile factory-operator]
credential_process = $HOME/.toolbox/bin/isengardcli credentials 045129524125 --role robcata --awscli
[profile factory-asana-sync]
role_arn = arn:aws:iam::045129524125:role/software-factory-asana-sync
source_profile = factory-operator
region = us-east-1
CFG
cp factory/sync/software-factory-asana-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now software-factory-asana-sync.timer
```

Without a systemd user session, cron works the same:
`*/15 * * * * cd ~/repos/sample-agentcore-enterprise-platform && set -a && . ~/.config/software-factory/asana-sync.env && AWS_CONFIG_FILE=~/.config/software-factory/aws-config AWS_PROFILE=factory-asana-sync PATH=$HOME/.toolbox/bin:$HOME/.local/bin:$PATH uv run -q --no-project -p 3.13 --with boto3 python factory/sync/asana_sync.py`

## Operate

```bash
python factory/sync/asana_sync.py --project <gid> --dry-run              # the board only, no AWS
AWS_PROFILE=factory-asana-sync python factory/sync/asana_sync.py --dry-run  # plus the pending ops, writes nothing
journalctl --user -u software-factory-asana-sync -n 20                  # last runs
```

Midway lasts about 20 hours. When it expires the MCP refuses, the sync exits 2, and six hours
later the agents skip with `stale_snapshot` rather than act on an old board. Run `mwinit` and the
next timer tick catches up; queued ops wait in the table meanwhile.

Tests: `cd factory && .venv/bin/python -m pytest sync -q`.
