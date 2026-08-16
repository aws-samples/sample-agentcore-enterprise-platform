"""Agents must derive caller identity from the verified JWT, never from a default.

Regression guard for the strands-agent fail-open: it set

    user_id = "anonymous"
    if hasattr(context, "identity") and context.identity:
        user_id = getattr(context.identity, "sub", "anonymous")

with no signature, issuer or client verification, and then passed user_id to
AgentCoreMemoryConfig as actor_id. Because actor_id is the tenant boundary for
stored conversation history, every unverified caller shared one actor's memory.

The fix is to call shared.auth.extract_user_id_from_context, which fails closed.
See docs/IDENTITY.md.

Parsed from source rather than imported: the CI test job has no
bedrock_agentcore or strands, and this is the cheapest thing that fails if
someone reintroduces a placeholder identity.
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

AGENT_CODE = REPO / "agent-code"

# The helper that performs full verification (signature, exp, iss, client, sub).
VERIFIER = "extract_user_id_from_context"


def _agent_files() -> list[Path]:
    return sorted(AGENT_CODE.glob("*/agent.py"))


def _takes_request_context(source: str) -> bool:
    """True if the entrypoint receives the RequestContext carrying the JWT.

    Agents without it (orchestrator, code-agent, research-agent) extract no
    identity at all and are guarded only by IAM, so there is nothing to assert.
    """
    return "context: RequestContext" in source


def test_agent_files_are_discovered():
    """Guard against the glob silently matching nothing."""
    assert len(_agent_files()) >= 6, (
        f"expected the agent patterns, got {_agent_files()}"
    )


def test_every_context_taking_agent_verifies_identity():
    offenders = []
    for path in _agent_files():
        source = path.read_text()
        if not _takes_request_context(source):
            continue
        if VERIFIER not in source:
            offenders.append(path.relative_to(REPO).as_posix())
    assert not offenders, (
        f"these agents accept a RequestContext but never call {VERIFIER}, so they "
        f"act on an unverified identity: {offenders}"
    )


def test_no_agent_assigns_a_placeholder_identity():
    """user_id must come from a call, never from a string literal.

    Catches the `user_id = "anonymous"` shape generically, including variants
    like "unknown" or "" that a future edit might reach for instead.
    """
    offenders = []
    for path in _agent_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "user_id" not in targets:
                continue
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                offenders.append(
                    f"{path.relative_to(REPO).as_posix()}:{node.lineno} "
                    f'user_id = "{node.value.value}"'
                )
    assert not offenders, (
        "identity must be derived from the verified token, not defaulted to a "
        f"literal: {offenders}"
    )


def test_agents_importing_shared_copy_it_into_the_image():
    """`from shared...` fails at container start unless the Dockerfile copies it.

    strands-agent imported nothing from shared/ before the identity fix, so its
    Dockerfile had no `COPY shared/ shared/` line and adding the import alone
    would have broken the image at module load.
    """
    offenders = []
    for path in _agent_files():
        if "from shared" not in path.read_text():
            continue
        dockerfile = path.parent / "Dockerfile"
        if not dockerfile.exists():
            offenders.append(f"{path.parent.name}: no Dockerfile")
            continue
        if "COPY shared/ shared/" not in dockerfile.read_text():
            offenders.append(f"{path.parent.name}: Dockerfile missing COPY shared/")
    assert not offenders, (
        f"these agents import shared/ but their image does not contain it: {offenders}"
    )
