"""Serve a Strands agent over the AgentCore A2A protocol contract.

AgentCore routes A2A traffic to a JSON-RPC server on **port 9000**, not to the
HTTP `/invocations` contract that BedrockAgentCoreApp implements. A sub-agent
built on the HTTP contract deploys fine and then returns HTTP 424 for every
invocation, with clean container logs — which is exactly how this went
unnoticed. Docs: "A2A server" (runtime-a2a-protocol-contract).

Strands' A2AServer provides `/` (POST, JSON-RPC) and
`/.well-known/agent-card.json` (GET). It does NOT provide `/ping`, which the
contract also requires, so this adds it.
"""

import logging

import uvicorn
from strands import Agent
from strands.multiagent.a2a import A2AServer

logger = logging.getLogger(__name__)

A2A_PORT = 9000  # fixed by the contract; not configurable
A2A_HOST = "0.0.0.0"  # nosec B104 — container-internal; AgentCore fronts it


def serve_a2a(agent: Agent, *, version: str = "1.0.0") -> None:
    """Serve `agent` on the A2A contract until the process is stopped."""
    server = A2AServer(
        agent=agent,
        host=A2A_HOST,
        port=A2A_PORT,
        # The contract puts JSON-RPC at the root path, not under a prefix.
        serve_at_root=True,
        version=version,
    )
    app = server.to_fastapi_app()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        """Health check.

        Deliberately returns no `time_of_last_update`. The field is optional,
        and a timestamp that advances on every ping reads as a continuous
        status change: the idle session timeout then never fires, sessions
        live to MaxLifetime, and the account's session quota drains. Omitting
        it lets the platform track status changes itself.
        """
        return {"status": "Healthy"}

    logger.info("[A2A] serving %s on %s:%d", agent.name, A2A_HOST, A2A_PORT)
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT)
