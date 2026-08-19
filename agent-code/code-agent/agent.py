"""Code Agent — A2A sub-agent for code generation and analysis.

Serves the AgentCore A2A protocol contract (JSON-RPC on port 9000), not the
HTTP `/invocations` contract — see shared/a2a_serve.py for why that matters.
"""

import logging
import os

from shared.a2a_serve import serve_a2a
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)

# Cross-region inference profile — current (not Legacy-flagged). Override per
# deployment via the MODEL_ID environment variable (see app.py / deploy.sh).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)

SYSTEM_PROMPT = """You are a code generation and analysis agent. You help with:
- Writing Python, TypeScript, and other code
- Code review and debugging
- Explaining code patterns and best practices
- Generating infrastructure-as-code templates"""


def build_agent() -> Agent:
    """The agent advertised on the A2A agent card and invoked by callers."""
    return Agent(
        name="code_agent",
        description="Generates, reviews, and explains code, including IaC templates.",
        system_prompt=SYSTEM_PROMPT,
        model=BedrockModel(model_id=MODEL_ID),
    )


if __name__ == "__main__":
    serve_a2a(build_agent())
