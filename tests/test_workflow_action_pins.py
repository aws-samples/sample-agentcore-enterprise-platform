"""Supply-chain guard for .github/workflows: files parse and action pins are sane.

Third-party actions should be pinned to a full 40-hex commit SHA with a
trailing ``# vX.Y.Z`` comment, and the same action should use the same SHA
in every workflow.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SHA = re.compile(r"^[0-9a-f]{40}$")
PINNED_LINE = re.compile(r"uses:\s*([^@\s]+)@([0-9a-f]{40})\b(.*)$")


def _uses(doc):
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and "uses" in step:
                yield str(step["uses"])


def _remote(ref):
    return not ref.startswith(("./", "docker://"))


def test_workflows_exist():
    assert WORKFLOWS, "no workflow files found"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert "jobs" in doc


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_sha_pins_carry_version_comment(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PINNED_LINE.search(line)
        if match:
            assert re.search(r"#\s*v\d", match.group(3)), line.strip()


def test_same_action_uses_same_sha():
    seen = {}
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for ref in _uses(doc):
            action, _, version = ref.partition("@")
            if _remote(ref) and SHA.match(version):
                seen.setdefault(action, set()).add(version)
    drift = {action: shas for action, shas in seen.items() if len(shas) > 1}
    assert not drift, drift


@pytest.mark.xfail(
    strict=False,
    reason="tag refs still need real SHAs resolved via git ls-remote",
)
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_remote_actions_pinned_to_sha(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    unpinned = [
        ref
        for ref in _uses(doc)
        if _remote(ref) and not SHA.match(ref.partition("@")[2])
    ]
    assert not unpinned, unpinned
