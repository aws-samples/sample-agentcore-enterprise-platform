"""A2A sub-agents must serve the A2A contract, not the HTTP one.

The defect these guard against was invisible from the outside: the sub-agents
were built on BedrockAgentCoreApp (HTTP `/invocations` on 8080) while AgentCore
routes A2A traffic to JSON-RPC on port 9000. Stacks deployed clean, container
logs were clean, and every invoke returned HTTP 424 — for as long as the A2A
runtimes had existed, because module 8's verify only read an SSM parameter.

Static guards (the CI test job has no strands/aws_cdk), in the style of
test_agent_identity.py.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
A2A_AGENTS = ("code-agent", "research-agent")
SERVE_HELPER = REPO / "agent-code" / "shared" / "a2a_serve.py"


def _agent_src(name: str) -> str:
    return (REPO / "agent-code" / name / "agent.py").read_text()


def _dockerfile(name: str) -> str:
    return (REPO / "agent-code" / name / "Dockerfile").read_text()


def test_the_serve_helper_pins_the_contract_port():
    src = SERVE_HELPER.read_text()
    assert "A2A_PORT = 9000" in src, "the contract fixes the port at 9000"
    assert 'A2A_HOST = "0.0.0.0"' in src


def test_the_serve_helper_adds_the_required_ping_endpoint():
    """Strands' A2AServer provides / and the agent card but NOT /ping."""
    src = SERVE_HELPER.read_text()
    assert '@app.get("/ping")' in src
    assert '"status": "Healthy"' in src


def test_ping_does_not_advance_a_timestamp():
    """A ping whose time_of_last_update moves every call reads as a continuous
    status change: the idle timeout never fires, sessions run to MaxLifetime,
    and the account's session quota drains. The field is optional — omit it."""
    src = SERVE_HELPER.read_text()
    ping_body = src.split('@app.get("/ping")', 1)[1]
    # Only the executable return statements matter — the docstring is allowed
    # (and expected) to explain the field we are deliberately not sending.
    returns = [
        line for line in ping_body.splitlines() if line.strip().startswith("return")
    ]
    assert returns, "the ping handler must return something"
    assert not any("time_of_last_update" in line for line in returns)


@pytest.mark.parametrize("name", A2A_AGENTS)
def test_a2a_agents_serve_a2a_not_http(name):
    src = _agent_src(name)
    assert "serve_a2a" in src, f"{name} must serve the A2A contract"
    assert "BedrockAgentCoreApp" not in src, (
        f"{name} uses BedrockAgentCoreApp — that is the HTTP contract on port "
        "8080, and AgentCore will return 424 for every A2A invoke"
    )


@pytest.mark.parametrize("name", A2A_AGENTS)
def test_a2a_agents_advertise_name_and_description(name):
    """Both land on the agent card that other agents discover."""
    src = _agent_src(name)
    assert re.search(r"name=\"[a-z_]+\"", src), f"{name}: agent needs a name"
    assert "description=" in src, f"{name}: agent needs a description"


@pytest.mark.parametrize("name", A2A_AGENTS)
def test_a2a_dockerfiles_expose_9000_and_healthcheck_it(name):
    body = _dockerfile(name)
    assert "EXPOSE 9000" in body, f"{name}: A2A serves on 9000"
    assert "8080" not in body, f"{name}: 8080 is the HTTP contract's port"
    assert "localhost:9000/ping" in body, (
        f"{name}: healthcheck must probe the A2A ping endpoint"
    )


@pytest.mark.parametrize("name", A2A_AGENTS)
def test_a2a_dockerfiles_copy_shared(name):
    """serve_a2a lives in shared/, so the image has to contain it."""
    assert "COPY shared/ shared/" in _dockerfile(name)


@pytest.mark.parametrize("name", A2A_AGENTS)
def test_a2a_requirements_include_the_a2a_extra(name):
    reqs = (REPO / "agent-code" / name / "requirements.txt").read_text()
    assert "strands-agents[a2a]" in reqs, (
        f"{name}: A2AServer needs the a2a extra (a2a-sdk), or the import fails "
        "at container start"
    )


def test_app_builds_both_a2a_agents_from_the_shared_context():
    """Both need shared/, so their build context is agent-code/ with a pattern."""
    src = (REPO / "app.py").read_text()
    for name in A2A_AGENTS:
        assert f'dockerfile_pattern="{name}"' in src, (
            f"{name} must build from the agent-code context to get shared/"
        )


def test_module_8_verify_actually_invokes():
    """The reason this defect survived: the verify only read an SSM parameter,
    so it passed against runtimes that could not be invoked at all."""
    src = (REPO / "scripts" / "deploy.sh").read_text()
    verify = re.search(r"MODULE_VERIFY\[8\]='([^']+)'", src)
    assert verify, "MODULE_VERIFY[8] not found"
    command = verify.group(1)
    assert "invoke.py" in command and "--a2a" in command, (
        f"module 8 must prove an A2A invoke works, got: {command}"
    )


def test_invoke_script_supports_a2a():
    src = (REPO / "scripts" / "invoke.py").read_text()
    assert '"--a2a"' in src
    assert '"message/send"' in src, "A2A invokes are JSON-RPC message/send"
