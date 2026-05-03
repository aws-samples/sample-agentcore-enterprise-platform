# Adapted from fullstack-solution-template-for-agentcore
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    DOCKER_CONTAINER=1 \
    OTEL_PYTHON_LOG_CORRELATION=true \
    PYTHONUNBUFFERED=1 \
    CLAUDE_CODE_USE_BEDROCK=1

# Install Node.js for claude-code CLI
RUN apt-get update && \
    apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install claude-code globally
RUN npm install -g @anthropic-ai/claude-code@2.1.73

# Copy and install agent-specific requirements
COPY claude-sdk-agent/requirements.txt requirements.txt
RUN uv pip install --no-cache -r requirements.txt && \
    uv pip install --no-cache aws-opentelemetry-distro==0.16.0

# Create non-root user and ensure claude config dir is writable
RUN useradd -m -u 1000 bedrock_agentcore && \
    mkdir -p /home/bedrock_agentcore/.claude && \
    chown -R bedrock_agentcore:bedrock_agentcore /home/bedrock_agentcore/.claude
USER bedrock_agentcore

ENV CLAUDE_CONFIG_DIR=/home/bedrock_agentcore/.claude

EXPOSE 8080

# Copy shared utilities and agent code
COPY shared/ shared/
COPY claude-sdk-agent/agent.py .
COPY claude-sdk-agent/code_int_mcp/ code_int_mcp/

# Healthcheck
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)" || exit 1

# Start agent with OpenTelemetry instrumentation
CMD ["opentelemetry-instrument", "python", "-m", "agent"]
