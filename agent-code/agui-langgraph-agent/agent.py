# Adapted from fullstack-solution-template-for-agentcore
"""AG-UI LangGraph agent with Gateway MCP tools, Memory, and Code Interpreter.

Uses copilotkit's LangGraphAGUIAgent to produce native AG-UI SSE events.
AgentCore proxies these unchanged when deployed with --protocol AGUI.
"""

import logging
import os

from ag_ui.core import RunAgentInput, RunErrorEvent
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from copilotkit import CopilotKitMiddleware, LangGraphAGUIAgent
from langchain.agents import create_agent
from langchain_aws import ChatBedrock
from langgraph_checkpoint_aws import AgentCoreMemorySaver
from shared.auth import extract_user_id_from_context
from tools.code_interpreter import LangGraphCodeInterpreterTools
from tools.gateway import create_gateway_mcp_client

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# Cross-region inference profile — current (not Legacy-flagged). Override per
# deployment via the MODEL_ID environment variable (see app.py / deploy.sh).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)

# When the platform enforces guardrailed-only Bedrock (security.require_guardrails),
# the runtime injects these and IAM denies any inference call without a guardrail.
_GUARDRAIL_KWARGS = (
    {
        "guardrails": {
            "guardrailIdentifier": os.environ["GUARDRAIL_ID"],
            "guardrailVersion": os.environ.get("GUARDRAIL_VERSION", "DRAFT"),
        }
    }
    if os.environ.get("GUARDRAIL_ID")
    else {}
)

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools via the Gateway and Code Interpreter. "
    "When asked about your tools, list them and explain what they do."
)


def _build_model() -> ChatBedrock:
    return ChatBedrock(
        model_id=MODEL_ID,
        temperature=0.1,
        streaming=True,
        beta_use_converse_api=True,
        **_GUARDRAIL_KWARGS,
    )


def _create_checkpointer() -> AgentCoreMemorySaver:
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")
    return AgentCoreMemorySaver(
        memory_id=memory_id,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


async def create_langgraph_agent():
    """Create a LangGraph agent with Gateway tools, Memory, and Code Interpreter."""
    tools = []
    try:
        mcp_client = await create_gateway_mcp_client()
        if mcp_client is not None:
            tools = await mcp_client.get_tools()
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any gateway failure
        logger.warning(
            "[AGENT] Gateway not available, continuing without gateway tools: %s", e
        )

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    code_tools = LangGraphCodeInterpreterTools(region)
    tools.append(code_tools.execute_python_securely)

    return create_agent(
        model=_build_model(),
        tools=tools,
        checkpointer=_create_checkpointer(),
        middleware=[CopilotKitMiddleware()],
        system_prompt=SYSTEM_PROMPT,
    )


@app.entrypoint
async def invocations(payload: dict, context: RequestContext):
    input_data = RunAgentInput.model_validate(payload)

    user_id = extract_user_id_from_context(context)

    # Build the graph before constructing the AG-UI wrapper: LangGraphAGUIAgent
    # inspects graph.nodes in __init__, so it cannot be handed graph=None and
    # filled in later. The entrypoint already runs per request, so the graph
    # (and its gateway token) is still created fresh for every invocation.
    agent = LangGraphAGUIAgent(
        name="agui_langgraph_agent",
        description="AG-UI LangGraph agent with Gateway MCP tools and Memory",
        graph=await create_langgraph_agent(),
        config={"configurable": {"actor_id": user_id}},
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
