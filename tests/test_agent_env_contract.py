"""Env-var contract between app.py and the agent code each runtime runs.

app.py hands every RuntimeStack an ``extra_env_vars`` dict. A variable set
there that no agent reads is dead config; a variable an agent requires
(``os.environ["X"]``) that nothing sets crashes the runtime. Both are caught
statically: app.py is parsed with ``ast`` and the agent code (the pattern
directory plus the shared/ modules it imports) is scanned for environment
reads, so no CDK synth or agent dependency is needed.
"""

import ast
import functools
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app.py"
AGENT_CODE = REPO / "agent-code"
SHARED = AGENT_CODE / "shared"

# AGENT_PATTERN options deployable to the orchestrator runtime.
ORCHESTRATOR_PATTERNS = (
    "orchestrator",
    "strands-agent",
    "langgraph-agent",
    "claude-sdk-agent",
    "claude-sdk-multi-agent",
    "agui-strands-agent",
    "agui-langgraph-agent",
)

# Required reads satisfied outside app.py's extra_env_vars (name: who sets it).
PLATFORM_INJECTED = {
    "PROJECT_NAME": "RuntimeStack always injects it",
    "ENVIRONMENT": "RuntimeStack always injects it",
    "AWS_REGION": "set by the AgentCore Runtime",
    "AWS_DEFAULT_REGION": "set by the AgentCore Runtime",
    "GUARDRAIL_ID": "RuntimeStack injects it when require_guardrails is on",
    "GUARDRAIL_VERSION": "RuntimeStack injects it when require_guardrails is on",
}

# Removed from app.py because no agent read them.
DEAD_VARS = ("STACK_NAME",)

# A quoted UPPER_CASE literal read via environ[...], getenv(...), any .get(...)
# (os.environ.get, source.get, env.get) or _env_setting(source, "NAME", ...).
_ANY_READ = re.compile(
    r"""(?:environ\[|getenv\(|\.get\(|_env_setting\(\s*\w+\s*,)"""
    r"""\s*["']([A-Z][A-Z0-9_]*)["']"""
)
_REQUIRED_READ = re.compile(
    r"""environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\](?!\s*=(?!=))"""
)
_SHARED_DOTTED = re.compile(r"^\s*(?:from|import)\s+shared\.(\w+)", re.MULTILINE)
_SHARED_FROM = re.compile(r"^\s*from\s+shared\s+import\s+\(?([\w, ]+)", re.MULTILINE)
_RELATIVE = re.compile(r"^\s*from\s+\.(\w+)\s+import", re.MULTILINE)


def _assignments(tree: ast.AST) -> dict[str, ast.AST]:
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
    return out


def _env_keys(node: ast.AST, assigns: dict[str, ast.AST]) -> set[str]:
    """String keys of every dict literal in node, following **name spreads."""
    if isinstance(node, ast.Name):
        return _env_keys(assigns[node.id], assigns) if node.id in assigns else set()
    keys = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Dict):
            for key, value in zip(child.keys, child.values, strict=True):
                if key is None:
                    keys |= _env_keys(value, assigns)
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


@functools.lru_cache
def _runtimes() -> dict[str, tuple[tuple[str, ...], frozenset[str]]]:
    """component_name -> (agent patterns it may run, env vars app.py sets)."""
    tree = ast.parse(APP.read_text())
    assigns = _assignments(tree)
    runtimes = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RuntimeStack"
        ):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        pattern = kwargs["dockerfile_pattern"]
        patterns = (
            (pattern.value,)
            if isinstance(pattern, ast.Constant)
            else ORCHESTRATOR_PATTERNS
        )
        env = (
            _env_keys(kwargs["extra_env_vars"], assigns)
            if "extra_env_vars" in kwargs
            else set()
        )
        runtimes[kwargs["component_name"].value] = (patterns, frozenset(env))
    return runtimes


def _shared_imports(path: Path, source: str) -> set[str]:
    names = set(_SHARED_DOTTED.findall(source))
    for group in _SHARED_FROM.findall(source):
        names.update(n.strip() for n in group.split(",") if n.strip())
    if path.parent == SHARED:
        names.update(_RELATIVE.findall(source))
    return names


def _agent_files(patterns: tuple[str, ...]) -> set[Path]:
    """Pattern sources plus the shared/ modules they (transitively) import."""
    files: set[Path] = set()
    for pattern in patterns:
        directory = AGENT_CODE / pattern
        assert directory.is_dir(), f"agent pattern directory missing: {directory}"
        files.update(directory.rglob("*.py"))
    pending = list(files)
    while pending:
        path = pending.pop()
        for name in _shared_imports(path, path.read_text()):
            candidate = SHARED / f"{name}.py"
            if candidate.is_file() and candidate not in files:
                files.add(candidate)
                pending.append(candidate)
    return files


def _reads(patterns: tuple[str, ...], regex: re.Pattern) -> set[str]:
    found = set()
    for path in _agent_files(patterns):
        found.update(regex.findall(path.read_text()))
    return found


RUNTIMES = ("orchestrator", "code-agent", "research-agent")


def test_app_py_runtimes_are_parsed():
    runtimes = _runtimes()
    assert set(RUNTIMES) <= set(runtimes)
    _, orchestrator_env = runtimes["orchestrator"]
    assert {"MEMORY_ID", "GATEWAY_URL", "MODEL_ID"} <= orchestrator_env


@pytest.mark.parametrize("component", RUNTIMES)
def test_every_env_var_app_sets_is_read(component):
    patterns, env = _runtimes()[component]
    unread = set(env) - _reads(patterns, _ANY_READ)
    assert not unread, (
        f"app.py sets {sorted(unread)} on the {component} runtime but no agent "
        f"pattern it runs ({', '.join(patterns)}) reads them"
    )


@pytest.mark.parametrize("component", RUNTIMES)
def test_every_required_env_var_is_set(component):
    patterns, env = _runtimes()[component]
    unset = _reads(patterns, _REQUIRED_READ) - set(env) - set(PLATFORM_INJECTED)
    assert not unset, (
        f"agents on the {component} runtime require {sorted(unset)} "
        "but app.py never sets them"
    )


def test_memory_retrieval_vars_are_set_and_read():
    ltm = {"USE_LONG_TERM_MEMORY", "LTM_TOP_K", "LTM_RELEVANCE_SCORE"}
    _, orchestrator_env = _runtimes()["orchestrator"]
    assert ltm <= orchestrator_env
    for pattern in ("strands-agent", "agui-strands-agent"):
        assert ltm <= _reads((pattern,), _ANY_READ), pattern


def test_dead_env_vars_stay_removed():
    for component in RUNTIMES:
        _, env = _runtimes()[component]
        assert not set(DEAD_VARS) & env, component
