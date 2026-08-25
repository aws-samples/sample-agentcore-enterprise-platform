"""The dashboard must agree with the CLI about what is deployed.

monitor.py owns status classification and scope; the browser renders what it
emits. These pin the three ways they used to disagree:

1. ROLLBACK_COMPLETE was counted as deployed AND failed (substring matches),
   which could make not_deployed negative.
2. The UI forced a denominator of at least 10, so a healthy greenfield
   deployment read 6/10 forever.
3. Module labels in monitor.py, index.html and deploy.sh all differed.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))

from monitor import STACK_META, classify

INDEX_HTML = (REPO / "dashboard" / "public" / "index.html").read_text()
DEPLOY_SH = (REPO / "scripts" / "deploy.sh").read_text()


def test_rollback_is_failed_and_only_failed():
    # The bug: "COMPLETE" in ROLLBACK_COMPLETE counted it deployed too.
    assert classify("ROLLBACK_COMPLETE") == "failed"
    assert classify("UPDATE_ROLLBACK_COMPLETE") == "failed"
    assert classify("CREATE_FAILED") == "failed"


def test_the_other_states():
    assert classify("CREATE_COMPLETE") == "deployed"
    assert classify("UPDATE_COMPLETE") == "deployed"
    assert classify("CREATE_IN_PROGRESS") == "in-progress"
    assert classify("NOT_DEPLOYED") == "not-deployed"
    assert classify("DELETE_COMPLETE") == "not-deployed"
    assert classify("NOT_APPLICABLE") == "not-applicable"
    assert classify("") == "not-deployed"
    assert classify(None) == "not-deployed"


def test_states_are_mutually_exclusive():
    # Every state maps to exactly one bucket, so counting can never
    # double-count and the summary can never go negative.
    for status in (
        "CREATE_COMPLETE",
        "ROLLBACK_COMPLETE",
        "DELETE_COMPLETE",
        "CREATE_IN_PROGRESS",
        "NOT_DEPLOYED",
        "NOT_APPLICABLE",
    ):
        assert classify(status) in {
            "deployed",
            "in-progress",
            "failed",
            "not-deployed",
            "not-applicable",
        }


def test_ui_takes_the_denominator_from_the_contract():
    # The fake floor is gone...
    assert "Math.max(allNames.length, 10)" not in INDEX_HTML
    # ...and the count comes from monitor.py's summary.
    assert "summary.total_stacks" in INDEX_HTML


def test_ui_prefers_the_emitted_state():
    assert (
        "if (typeof stack === 'object' && stack?.state) return stack.state;"
        in INDEX_HTML
    )


def test_module_labels_match_deploy_sh():
    # MODULE_MAP[<id>]="<prefix>-<suffix> ..." — the CLI's own mapping.
    mapping: dict[str, set[str]] = {}
    for module, stacks in re.findall(r'MODULE_MAP\[(\w+)\]="([^"]*)"', DEPLOY_SH):
        for stack in stacks.split():
            mapping.setdefault(stack.replace("${PREFIX}-", ""), set()).add(module)
    for suffix, meta in STACK_META.items():
        if suffix in mapping:
            assert meta["module"] in mapping[suffix], (
                f"{suffix}: dashboard says module {meta['module']}, "
                f"deploy.sh says {sorted(mapping[suffix])}"
            )
