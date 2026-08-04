"""Strands agent with Gateway MCP tools and Memory."""

import json
import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.models import BedrockModel

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# Cross-region inference profile — current (not Legacy-flagged). Override per
# deployment via the MODEL_ID environment variable (see app.py / deploy.sh).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)

SYSTEM_PROMPT = (
    "You are a helpful assistant for the AgentCore Workshop. "
    "You can help with research, analysis, and general questions. "
    "Be concise and helpful."
)


def _create_agent(user_id: str, session_id: str) -> Agent:
    """Create a Strands agent with memory support."""
    model = BedrockModel(
        model_id=MODEL_ID,
        temperature=0.1,
    )

    memory_id = os.environ.get("MEMORY_ID", "")

    kwargs = {}
    if memory_id:
        try:
            from bedrock_agentcore.memory.integrations.strands.config import (
                AgentCoreMemoryConfig,
            )
            from bedrock_agentcore.memory.integrations.strands.session_manager import (
                AgentCoreMemorySessionManager,
            )

            config = AgentCoreMemoryConfig(
                memory_id=memory_id,
                session_id=session_id,
                actor_id=user_id,
            )
            kwargs["session_manager"] = AgentCoreMemorySessionManager(
                agentcore_memory_config=config,
                region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            )
            logger.info("Memory enabled: %s", memory_id)
        except Exception as e:  # noqa: BLE001 — degrade gracefully on any memory failure
            logger.warning("Memory init failed (continuing without): %s", e)

    return Agent(
        name="workshop_agent",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        **kwargs,
    )


@app.entrypoint
async def invocations(payload, context: RequestContext):
    """Main entrypoint — called by AgentCore Runtime on each request."""
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")

    if not all([user_query, session_id]):
        yield {
            "status": "error",
            "error": "Missing required fields: prompt or runtimeSessionId",
        }
        return

    try:
        # Extract user ID from JWT context
        user_id = "anonymous"
        if hasattr(context, "identity") and context.identity:
            user_id = getattr(context.identity, "sub", "anonymous")

        agent = _create_agent(user_id, session_id)

        async for event in agent.stream_async(user_query):
            yield json.loads(json.dumps(dict(event), default=str))

    except Exception as e:
        logger.exception("Agent run failed")
        yield {"status": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
