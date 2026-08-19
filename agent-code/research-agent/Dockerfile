FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY research-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir aws-opentelemetry-distro==0.16.0
COPY shared/ shared/
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore
# 9000 and /ping are fixed by the AgentCore A2A protocol contract.
EXPOSE 9000
COPY research-agent/agent.py .
COPY research-agent/tools/ tools/
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/ping', timeout=2)" || exit 1
CMD ["opentelemetry-instrument", "python", "agent.py"]
