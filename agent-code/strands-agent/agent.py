"""Strands agent with Gateway MCP tools, Memory, and Code Interpreter."""

import json
import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from shared.auth import extract_user_id_from_context
from strands import Agent
from strands.models import BedrockModel
from tools.code_interpreter import StrandsCodeInterpreterTools
from tools.gateway import create_gateway_mcp_client

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# Cross-region inference profile — current (not Legacy-flagged). Override per
# deployment via the MODEL_ID environment variable (see app.py / deploy.sh).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools via the Gateway and Code Interpreter. "
    "When asked about your tools, list them and explain what they do."
)


def _create_agent(user_id: str, session_id: str) -> Agent:
    """Create a Strands agent with Gateway MCP tools, Memory, and Code Interpreter."""
    tools = []

    # Gateway MCP tools (degrades gracefully if not configured)
    try:
        gateway_client = create_gateway_mcp_client()
        if gateway_client is not None:
            tools.append(gateway_client)
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any gateway failure
        logger.warning(
            "[AGENT] Gateway not available, continuing without gateway tools: %s", e
        )

    # Code Interpreter
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    code_tools = StrandsCodeInterpreterTools(region)
    tools.append(code_tools.execute_python_securely)

    # Memory (optional — degrade gracefully if not configured)
    kwargs = {}
    memory_id = os.environ.get("MEMORY_ID", "")
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
                region_name=region,
            )
            logger.info("Memory enabled: %s", memory_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Memory init failed (continuing without): %s", e)

    model = BedrockModel(model_id=MODEL_ID, temperature=0.1)

    return Agent(
        name="strands_agent",
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
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
        # Identity comes from the verified JWT, never from the payload. This is
        # the Memory actor_id below, so an unverified fallback would let every
        # caller share one actor's conversation history.
        user_id = extract_user_id_from_context(context)

        agent = _create_agent(user_id, session_id)

        async for event in agent.stream_async(user_query):
            yield json.loads(json.dumps(dict(event), default=str))

    except ValueError as e:
        # Token missing, malformed, or failed verification. Log the reason,
        # return a generic rejection so the response leaks no auth detail.
        logger.warning("Rejected unverified caller: %s", e)
        yield {"status": "error", "error": "Unauthorized"}

    except Exception as e:
        logger.exception("Agent run failed")
        yield {"status": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
