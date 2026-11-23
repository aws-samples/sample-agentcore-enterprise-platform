# Asana sync

The factory's only path to the Asana board. AWS holds no Asana credential: the agents queue board
changes in `software-factory-asana-ops` and read the board from `software-factory-asana-snapshot`.
This script, run by an operator, moves both through an Asana MCP server under the operator's own
Asana session. See `agent-code/factory/asana.py` for the board flow.

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
`Asana-Task: <task gid>` in the PR body.

## Run

Needs Python 3.13 with boto3, an Asana MCP server that speaks stdio, and AWS credentials that can
assume the `software-factory-asana-sync` role (stack output `AsanaSyncRoleArn`).

```bash
export FACTORY_ASANA_PROJECT=<project gid> FACTORY_ASANA_MCP="<command that starts the MCP server>"
python factory/sync/asana_sync.py --dry-run                       # the board only, no AWS
AWS_PROFILE=<sync profile> python factory/sync/asana_sync.py --dry-run  # plus pending ops, writes nothing
AWS_PROFILE=<sync profile> python factory/sync/asana_sync.py      # apply ops, refresh the snapshot
```

Schedule the last command every 15 minutes (a systemd timer or cron). If the operator's session
expires the sync exits 2; queued ops wait in the table, and six hours later the agents skip with
`stale_snapshot` rather than act on an old board. Renew the session and the next run catches up.

Tests: `python -m pytest factory/sync -q`.
