"""Research Agent — A2A sub-agent with Gateway tools (web search)."""

import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from tools.gateway import create_gateway_mcp_client

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# Cross-region inference profile — current (not Legacy-flagged). Override per
# deployment via the MODEL_ID environment variable (see app.py / deploy.sh).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)

SYSTEM_PROMPT = """You are a research agent. You help with:
- Gathering and synthesizing information, using the web search tool for
  anything current or outside your training data
- Analyzing documents and data
- Providing summaries and insights
- Answering factual questions with citations (cite result URLs when you
  used web search)"""


def _create_agent() -> Agent:
    """Research agent with Gateway tools (web search when the target exists)."""
    tools = []
    try:
        gateway_client = create_gateway_mcp_client()
        if gateway_client is not None:
            tools.append(gateway_client)
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any gateway failure
        logger.warning(
            "[AGENT] Gateway not available, continuing without gateway tools: %s", e
        )
    return Agent(
        system_prompt=SYSTEM_PROMPT,
        model=BedrockModel(model_id=MODEL_ID),
        tools=tools,
    )


@app.entrypoint
async def invoke(payload=None):
    query = payload.get("prompt", "Hello!") if payload else "Hello!"
    agent = _create_agent()
    response = agent(query)
    return {"status": "success", "response": response.message["content"][0]["text"]}


if __name__ == "__main__":
    app.run()
