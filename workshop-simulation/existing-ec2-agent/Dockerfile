# The simulated customer container — a plain agent service with its own HTTP
# contract (POST /run, GET /healthz on :8000). The migration adapter is built
# FROM this image; nothing here knows about AgentCore.
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN groupadd --gid 10001 migrated-agent \
    && useradd --uid 10001 --gid 10001 --no-create-home migrated-agent
COPY --chown=10001:10001 agent.py server.py ./
EXPOSE 8000
USER 10001:10001
CMD ["python", "server.py"]
