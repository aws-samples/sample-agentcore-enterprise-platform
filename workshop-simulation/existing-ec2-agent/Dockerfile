# The simulated customer container — a plain agent service with its own HTTP
# contract (POST /run, GET /healthz on :8000). The migration adapter is built
# FROM this image; nothing here knows about AgentCore.
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py server.py ./
EXPOSE 8000
CMD ["python", "server.py"]
