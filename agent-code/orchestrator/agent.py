"""Workshop orchestrator agent — deployed to AgentCore Runtime.

A single Strands agent with no tools: it answers directly from the model and
does not call the A2A sub-agents. The blocking model call runs in a worker
thread so the runtime's /ping health check is served during long requests.
"""

import asyncio
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

# Cross-region inference profile — current (not Legacy-flagged). Override per
# deployment via the MODEL_ID environment variable (see app.py / deploy.sh).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
MODEL_ID = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)


def _guardrail_version() -> str:
    """Return the guardrail version to attach, which must be published and pinned.

    Production never falls back to DRAFT: a missing version fails start-up.
    A workshop may use DRAFT only by opting in explicitly with
    GUARDRAIL_VERSION=DRAFT and GUARDRAIL_ALLOW_DRAFT=true. The opt-in is
    ignored when ENVIRONMENT is prod.
    """
    version = os.environ.get("GUARDRAIL_VERSION", "").strip()
    environment = os.environ.get("ENVIRONMENT", "").strip().lower()
    allow_draft = (
        environment not in ("prod", "production")
        and os.environ.get("GUARDRAIL_ALLOW_DRAFT", "").lower() == "true"
    )
    if not version or (version.upper() == "DRAFT" and not allow_draft):
        raise RuntimeError(
            "GUARDRAIL_ID is set but GUARDRAIL_VERSION is not a published version "
            f"(got {version!r}); DRAFT needs GUARDRAIL_ALLOW_DRAFT=true, which is "
            "workshop only and ignored when ENVIRONMENT is prod"
        )
    return version


# When the platform enforces guardrailed-only Bedrock (security.require_guardrails),
# the runtime injects these and IAM denies any inference call without a guardrail.
_GUARDRAIL_KWARGS = (
    {
        "guardrail_id": os.environ["GUARDRAIL_ID"],
        "guardrail_version": _guardrail_version(),
    }
    if os.environ.get("GUARDRAIL_ID")
    else {}
)

SYSTEM_PROMPT = """You are a helpful workshop assistant for the AgentCore Platform Accelerator.
You help participants understand and use Amazon Bedrock AgentCore services."""


def _answer(query: str) -> str:
    """Build the agent and run it synchronously (call off the event loop)."""
    model = BedrockModel(model_id=MODEL_ID, **_GUARDRAIL_KWARGS)
    agent = Agent(system_prompt=SYSTEM_PROMPT, model=model)
    response = agent(query)
    return response.message["content"][0]["text"]


@app.entrypoint
async def invoke(payload=None):
    query = payload.get("prompt", "Hello!") if payload else "Hello!"
    text = await asyncio.to_thread(_answer, query)
    return {"status": "success", "response": text}


if __name__ == "__main__":
    app.run()
