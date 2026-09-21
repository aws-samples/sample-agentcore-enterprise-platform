# Migration capability

Everything specific to adapting an existing customer agent lives under this
capability root:

| Directory | Responsibility |
|---|---|
| `adapter/` | Wrap an existing container with the AgentCore Runtime contract |
| `network-probe/` | Verify declared private dependencies from the runtime VPC |
| `simulation/` | Provide the synthetic existing-agent fixture and EBA walkthrough |

Migration remains opt-in through `platform.yaml`. These directories do not
copy customer data, change customer traffic, or modify customer triggers.
See the [migration runbook](../docs/MIGRATION_RUNBOOK.md) for the supported
workflow and responsibility boundaries.
