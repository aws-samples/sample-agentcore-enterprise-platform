# Claude apps gateway runtime image (linux/arm64, ECS Fargate on Graviton).
#
# Follows Anthropic's AWS example (anthropics/claude-code examples/gateway/aws)
# with one deliberate difference: gateway.yaml is NOT baked in. The image holds
# only the verified binary and the RDS CA bundle, so it changes only when the
# pinned Claude Code version does. The config is written at task start by an
# init container into a task-scoped volume mounted read-only at /config (see
# stack.py), so editing models, teams or policies in platform.yaml is a task
# definition change and a rolling deploy, not an image rebuild.
#
# CodeBuild (stack.py) downloads the pinned release's linux-arm64 `claude`,
# verifies it against the GPG-signed manifest.json, fetches the AWS RDS CA
# bundle, and places both next to this file.

# distroless/cc: glibc + libstdc++, no shell, no package manager, uid 65532.
# Pinned by digest (multi-arch index; --platform picks arm64). Same pin as
# Anthropic's example. Refresh after review:
#   docker manifest inspect -v gcr.io/distroless/cc-debian12:nonroot
FROM gcr.io/distroless/cc-debian12:nonroot@sha256:ce0d66bc0f64aae46e6a03add867b07f42cc7b8799c949c2e898057b7f75a151

COPY --chmod=0755 claude /usr/local/bin/claude
WORKDIR /etc/claude
COPY --chmod=0644 rds-global-bundle.pem /etc/claude/rds-global-bundle.pem
WORKDIR /

# Writable state dir (distroless has no writable home) and the trust anchor
# for store.postgres_url ?sslmode=verify-full.
ENV CLAUDE_CONFIG_DIR=/tmp/.claude \
    NODE_EXTRA_CA_CERTS=/etc/claude/rds-global-bundle.pem

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/claude", "gateway", "--config", "/config/gateway.yaml"]
