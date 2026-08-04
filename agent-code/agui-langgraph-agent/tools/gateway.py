# Adapted from fullstack-solution-template-for-agentcore
"""AgentCore Gateway MCP client with OAuth2 authentication (async, for LangGraph)."""

import logging
import os

from bedrock_agentcore.identity.auth import requires_access_token
from langchain_mcp_adapters.client import MultiServerMCPClient
from shared.ssm import get_ssm_parameter

logger = logging.getLogger(__name__)

_PROVIDER_NAME = os.environ.get("GATEWAY_CREDENTIAL_PROVIDER_NAME", "")


def _gateway_url_ssm_path() -> str | None:
    """Build the conventional SSM path for the gateway URL.

    Uses the PROJECT_NAME and ENVIRONMENT variables that RuntimeStack always
    injects, matching the path published by the gateway stack. Returns None
    when either variable is missing or has an unexpected format.
    """
    project_name = os.environ.get("PROJECT_NAME", "")
    environment = os.environ.get("ENVIRONMENT", "")
    for name, value in (("PROJECT_NAME", project_name), ("ENVIRONMENT", environment)):
        if not value:
            logger.warning("[GATEWAY] %s environment variable is not set", name)
            return None
        if not value.replace("-", "").replace("_", "").isalnum():
            logger.warning("[GATEWAY] Invalid %s format", name)
            return None
    return f"/{project_name}/{environment}/gateway/url"


def _resolve_gateway_url() -> str | None:
    """Resolve the gateway URL from the environment or SSM Parameter Store.

    Prefers the GATEWAY_URL environment variable (injected into the
    orchestrator runtime — no SSM call or ssm:GetParameter permission
    needed). Falls back to the conventional SSM parameter
    /{PROJECT_NAME}/{ENVIRONMENT}/gateway/url. Returns None when neither
    source is available so callers can degrade gracefully.
    """
    gateway_url = os.environ.get("GATEWAY_URL", "")
    if gateway_url:
        return gateway_url

    ssm_path = _gateway_url_ssm_path()
    if ssm_path is None:
        logger.warning(
            "[GATEWAY] GATEWAY_URL not set and conventional SSM path "
            "unavailable — gateway URL cannot be resolved"
        )
        return None
    return get_ssm_parameter(ssm_path)


async def _fetch_gateway_token() -> str:
    """Fetch OAuth2 token for Gateway authentication.

    The @requires_access_token decorator handles token retrieval and refresh.
    It's applied lazily at call time (not import time) so a missing
    GATEWAY_CREDENTIAL_PROVIDER_NAME doesn't crash container startup.
    Async because it's awaited in create_gateway_mcp_client().
    """

    @requires_access_token(
        provider_name=_PROVIDER_NAME,
        auth_flow="M2M",
        scopes=[],
    )
    async def _get_token(access_token: str) -> str:
        return access_token

    return await _get_token()


async def create_gateway_mcp_client() -> MultiServerMCPClient | None:
    """Create MCP client for AgentCore Gateway with OAuth2 authentication.

    Fetches a fresh token per call (called per-request in agent entrypoint).

    Returns None (gateway tools disabled) when the credential provider
    name is not configured or the gateway URL cannot be resolved.
    """
    if not _PROVIDER_NAME:
        logger.warning(
            "[GATEWAY] GATEWAY_CREDENTIAL_PROVIDER_NAME not set — gateway tools disabled"
        )
        return None

    gateway_url = _resolve_gateway_url()
    if gateway_url is None:
        return None
    logger.info("[GATEWAY] URL: %s", gateway_url)

    fresh_token = await _fetch_gateway_token()

    return MultiServerMCPClient(
        {
            "gateway": {
                "transport": "streamable_http",
                "url": gateway_url,
                "headers": {"Authorization": f"Bearer {fresh_token}"},
            }
        }
    )
