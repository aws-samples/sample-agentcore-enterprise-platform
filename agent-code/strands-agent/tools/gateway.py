# Adapted from fullstack-solution-template-for-agentcore
"""AgentCore Gateway MCP client with OAuth2 authentication."""

import logging
import os

from bedrock_agentcore.identity.auth import requires_access_token
from mcp.client.streamable_http import streamablehttp_client
from shared.ssm import get_ssm_parameter
from strands.tools.mcp import MCPClient

logger = logging.getLogger(__name__)

_PROVIDER_NAME = os.environ.get("GATEWAY_CREDENTIAL_PROVIDER_NAME", "")


def _fetch_gateway_token() -> str:
    """Fetch OAuth2 token for Gateway authentication.

    The @requires_access_token decorator handles token retrieval and refresh.
    It's applied lazily at call time (not import time) so a missing
    GATEWAY_CREDENTIAL_PROVIDER_NAME doesn't crash container startup.
    Must be synchronous — called inside the MCPClient lambda factory.
    """

    @requires_access_token(
        provider_name=_PROVIDER_NAME,
        auth_flow="M2M",
        scopes=[],
    )
    def _get_token(access_token: str) -> str:
        return access_token

    return _get_token()


def create_gateway_mcp_client() -> MCPClient | None:
    """Create MCP client for AgentCore Gateway with OAuth2 authentication.

    Returns None (gateway tools disabled) when the credential provider
    name is not configured.
    """
    if not _PROVIDER_NAME:
        logger.warning(
            "[GATEWAY] GATEWAY_CREDENTIAL_PROVIDER_NAME not set — gateway tools disabled"
        )
        return None

    stack_name = os.environ.get("STACK_NAME")
    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")
    if not stack_name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid STACK_NAME format")

    gateway_url = get_ssm_parameter(f"/{stack_name}/gateway_url")
    logger.info("[GATEWAY] URL: %s", gateway_url)

    return MCPClient(
        lambda: streamablehttp_client(
            url=gateway_url,
            headers={"Authorization": f"Bearer {_fetch_gateway_token()}"},
        ),
        prefix="gateway",
    )
