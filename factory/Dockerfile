# Software factory runtime image (AgentCore Runtime, ARM64, HTTP on 8080).
# Build context is the repository root, filtered to factory/ and requirements.txt.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:22-bookworm-slim AS node

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g aws-cdk@2.1148.1
# The repo's own CDK/test dependencies, so `cdk synth` and pytest run without a per-run install.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt ruff
RUN useradd -r -u 1001 -m factory
COPY factory/agent-code /app
WORKDIR /app
USER factory
ENV HOME=/tmp/home PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "factory.server"]
