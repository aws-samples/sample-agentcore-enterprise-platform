"""Cost attribution rests on the Component tag actually being applied.

app.py tags every stack with Component = its contract suffix (the same
vocabulary as expected_stacks). If the tagging loop disappears, Cost Explorer
silently loses the per-component split and nothing else fails — so pin it
here, source-level, the same way test_runtime_role_scoping pins IAM intent
(the CI pytest job has no aws_cdk to synthesize with).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app.py"


def test_every_stack_gets_a_component_tag():
    source = APP.read_text()
    assert '.add(\n            "Component"' in source or '.add("Component"' in source, (
        "app.py must tag stacks with Component for cost attribution"
    )
    # The tag value must stay in the contract's vocabulary: the stack id minus
    # the project-environment prefix, exactly what expected_stacks() emits.
    assert '_stack.node.id.removeprefix(f"{prefix}-")' in source
    # And it must run over ALL stacks (including use-case stacks), not a
    # hand-maintained list that new stacks silently miss.
    assert "for _stack in app.node.children" in source
