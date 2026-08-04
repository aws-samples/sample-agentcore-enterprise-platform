# Adapted from fullstack-solution-template-for-agentcore
"""AG-UI Strands agent with Gateway MCP tools, Memory, and Code Interpreter.

Uses ag-ui-strands to produce native AG-UI SSE events.
AgentCore proxies these unchanged when deployed with --protocol AGUI.
"""

import logging
import os

from ag_ui.core import RunAgentInput, RunErrorEvent
from ag_ui_strands import StrandsAgent
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
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


def _build_model() -> BedrockModel:
    return BedrockModel(model_id=MODEL_ID, temperature=0.1)


def _create_session_manager(
    user_id: str, session_id: str
) -> AgentCoreMemorySessionManager:
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")
    config = AgentCoreMemoryConfig(
        memory_id=memory_id, session_id=session_id, actor_id=user_id
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def _create_agent(user_id: str, session_id: str) -> Agent:
    """Create a Strands Agent with Gateway MCP tools, Memory, and Code Interpreter."""
    tools = []
    try:
        gateway_client = create_gateway_mcp_client()
        if gateway_client is not None:
            tools.append(gateway_client)
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any gateway failure
        logger.warning(
            "[AGENT] Gateway not available, continuing without gateway tools: %s", e
        )

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    code_tools = StrandsCodeInterpreterTools(region)
    tools.append(code_tools.execute_python_securely)

    return Agent(
        name="strands_agent",
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        model=_build_model(),
        session_manager=_create_session_manager(user_id, session_id),
    )


class ActorAwareStrandsAgent(StrandsAgent):
    """StrandsAgent that creates the agent per-request with fresh MCP context."""

    def __init__(self, *, user_id: str, session_id: str, name: str, description: str):
        self._user_id = user_id
        self._session_id = session_id
        super().__init__(
            agent=Agent(model=_build_model(), system_prompt=SYSTEM_PROMPT),
            name=name,
            description=description,
        )

    async def run(self, input_data: RunAgentInput):
        thread_id = input_data.thread_id or "default"
        self._agents_by_thread[thread_id] = _create_agent(
            self._user_id, self._session_id
        )
        async for event in super().run(input_data):
            yield event


@app.entrypoint
async def invocations(payload: dict, context: RequestContext):
    input_data = RunAgentInput.model_validate(payload)
    user_id = extract_user_id_from_context(context)

    agent = ActorAwareStrandsAgent(
        user_id=user_id,
        session_id=input_data.thread_id,
        name="agui_strands_agent",
        description="AG-UI Strands agent with Gateway MCP tools and Code Interpreter",
    )

    try:
        async for event in agent.run(input_data):
            if event is not None:
                yield event.model_dump(mode="json", by_alias=True, exclude_none=True)
    except Exception as exc:
        logger.exception("Agent run failed")
        yield RunErrorEvent(
            message=str(exc) or type(exc).__name__,
            code=type(exc).__name__,
        ).model_dump(mode="json", by_alias=True, exclude_none=True)


if __name__ == "__main__":
    app.run()
