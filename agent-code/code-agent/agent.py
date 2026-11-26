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


def _guardrail_version() -> str:
    """Return the guardrail version to attach, which must be published and pinned.

    Production never falls back to DRAFT: a missing version fails start-up.
    A workshop may use DRAFT only by opting in explicitly with
    GUARDRAIL_VERSION=DRAFT and GUARDRAIL_ALLOW_DRAFT=true.
    """
    version = os.environ.get("GUARDRAIL_VERSION", "").strip()
    allow_draft = os.environ.get("GUARDRAIL_ALLOW_DRAFT", "").lower() == "true"
    if not version or (version.upper() == "DRAFT" and not allow_draft):
        raise RuntimeError(
            "GUARDRAIL_ID is set but GUARDRAIL_VERSION is not a published version "
            f"(got {version!r}); DRAFT needs GUARDRAIL_ALLOW_DRAFT=true (workshop only)"
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
        model=BedrockModel(model_id=MODEL_ID, **_GUARDRAIL_KWARGS),
    )


if __name__ == "__main__":
    serve_a2a(build_agent())
