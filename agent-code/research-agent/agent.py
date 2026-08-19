"""Research Agent — A2A sub-agent with Gateway tools (web search).

Serves the AgentCore A2A protocol contract (JSON-RPC on port 9000), not the
HTTP `/invocations` contract — see shared/a2a_serve.py for why that matters.
"""

import logging
import os

from shared.a2a_serve import serve_a2a
from strands import Agent
from strands.models import BedrockModel
from tools.gateway import create_gateway_mcp_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


def build_agent() -> Agent:
    """Research agent with Gateway tools (web search when the target exists).

    The gateway MCP client is built once at startup rather than per request:
    its OAuth token is fetched inside the client factory on each session, so a
    long-lived process still gets fresh credentials.
    """
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
        name="research_agent",
        description="Researches topics using web search and cites its sources.",
        system_prompt=SYSTEM_PROMPT,
        model=BedrockModel(model_id=MODEL_ID),
        tools=tools,
    )


if __name__ == "__main__":
    serve_a2a(build_agent())
