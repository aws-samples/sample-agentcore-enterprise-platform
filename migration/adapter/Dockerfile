# Migration adapter — TEMPLATE, built by the migration buildspec in
# stacks/runtime_stack.py (not by hand, and not selectable as an agent pattern):
#
#   docker build --platform linux/arm64 \
#     --build-arg SOURCE_IMAGE=<customer image ref or local intermediate tag> \
#     --build-arg CHILD_CMD='["python","server.py"]' \
#     --build-arg CHILD_USER='<source image Config.User, or root when empty>' \
#     -f adapter/Dockerfile adapter/
#
# CHILD_CMD is the source image's Entrypoint+Cmd as ONE JSON array, captured
# with `docker inspect` at build time. CHILD_USER is Config.User from the same
# inspection, normalized to "root" when empty. The adapter takes over
# CMD/ENTRYPOINT to serve the AgentCore contract, then runs the original
# command as a child under that original user.
# AgentCore Runtime is arm64-only: the source image must have an arm64 variant
# (or be rebuilt from source via migration.source.build).
ARG SOURCE_IMAGE
FROM ${SOURCE_IMAGE}

# Installing python3 + the adapter deps needs root. CHILD_USER restores the
# source image's configured user after installation. The runtime guard in
# adapter.py refuses to start as root when CHILD_USER declared a non-root user.
USER root

# Ensure python3 + pip exist regardless of the source image's distro family.
RUN if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then \
        if command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates && rm -rf /var/lib/apt/lists/*; \
        elif command -v apk >/dev/null 2>&1; then \
            apk add --no-cache python3 py3-pip ca-certificates; \
        elif command -v microdnf >/dev/null 2>&1; then \
            microdnf install -y python3 python3-pip ca-certificates && microdnf clean all; \
        else \
            echo "ERROR: no supported package manager (apt/apk/microdnf) to install python3" && exit 1; \
        fi; \
    fi

# Same floor the agent patterns pin (agent-code/strands-agent/requirements.txt).
# --break-system-packages: Debian/Ubuntu-based source images mark the system
# python externally managed (PEP 668); pip<23 ignores the unknown flag path via
# fallback, so try with it first and retry without.
RUN python3 -m pip install --no-cache-dir --break-system-packages "bedrock-agentcore>=1.21.0" boto3 \
    || python3 -m pip install --no-cache-dir "bedrock-agentcore>=1.21.0" boto3

COPY adapter.py /opt/adapter/adapter.py

ARG CHILD_CMD
ENV ADAPTER_CHILD_CMD=${CHILD_CMD}

ARG CHILD_USER=root
RUN case "${CHILD_USER}" in \
        ""|*[!A-Za-z0-9_.:-]*) \
            echo "ERROR: CHILD_USER must be a user or uid optionally followed by :group/:gid" && exit 1 ;; \
    esac
ENV ADAPTER_CHILD_USER=${CHILD_USER}

# The adapter owns the process tree: clear any inherited ENTRYPOINT so CMD is
# the whole command (otherwise the source image's entrypoint wraps it).
ENTRYPOINT []
EXPOSE 8080
USER ${CHILD_USER}
CMD ["python3", "/opt/adapter/adapter.py"]
