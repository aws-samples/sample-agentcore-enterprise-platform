"""Image tag must change when the selected agent pattern changes.

Regression guard: the tag doubles as the CodeBuild rebuild trigger. When it
ignored dockerfile_pattern, switching agent_pattern silently reused the
previous pattern's container image (found during live pattern testing).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stacks.runtime_stack import _component_image_tag, _compute_source_hash

AGENT_CODE = Path(__file__).resolve().parents[1] / "agent-code"


def test_pattern_changes_the_tag():
    langgraph = _component_image_tag(str(AGENT_CODE), "langgraph-agent")
    strands = _component_image_tag(str(AGENT_CODE), "strands-agent")
    assert langgraph != strands, (
        "each pattern needs its own tag, or no rebuild is triggered"
    )


def test_same_inputs_are_stable():
    """Unchanged source + pattern must not churn the tag (no spurious rebuilds)."""
    first = _component_image_tag(str(AGENT_CODE), "langgraph-agent")
    assert first == _component_image_tag(str(AGENT_CODE), "langgraph-agent")


def test_no_pattern_falls_back_to_content_hash():
    """Runtimes without a pattern (code-agent, research-agent) keep the old tag."""
    assert _component_image_tag(str(AGENT_CODE)) == _compute_source_hash(
        str(AGENT_CODE)
    )
