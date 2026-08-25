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


GRAPH_JS = (REPO / "dashboard" / "public" / "graph.js").read_text()


def test_graph_topology_only_names_real_stacks():
    # The architecture graph hardcodes a topology mirroring app.py's stack
    # dependencies. Every id in it must be a stack the poller knows about, or
    # the graph draws an edge to a node that can never have a status.
    ids = set()
    body = GRAPH_JS.split("var TOPOLOGY = {", 1)[1].split("};", 1)[0]
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, targets = line.split(":", 1)
        ids.add(key.strip().strip("',\""))
        ids.update(t.strip().strip("',\"[] ") for t in targets.split(","))
    ids.discard("")
    unknown = sorted(ids - set(STACK_META))
    assert not unknown, (
        f"graph.js topology names stacks monitor.py does not have: {unknown}"
    )


def test_graph_reads_the_emitted_state():
    # Same rule as the Monitor tab: one classifier, and it lives in monitor.py.
    # (The name may appear in a comment pointing at monitor.classify — what must
    # not exist is a second implementation.)
    assert "rec.state" in GRAPH_JS
    assert "function classify" not in GRAPH_JS


def test_graph_never_invents_status_for_unpolled_accounts():
    # A dashboard sees one account; the other side of a federation must be
    # drawn as unknown rather than assumed missing.
    assert "unobserved" in GRAPH_JS
    assert "not observed" in GRAPH_JS.lower()


def test_architecture_tab_mounts_only_when_visible():
    # A display:none canvas has clientWidth 0 — mounting then sizes the graph
    # to its minimum floor and it never recovers until a resize.
    tab_section = INDEX_HTML.split("function switchTab", 1)[1][:1600]
    assert "classList.add('active')" in tab_section
    assert tab_section.index("classList.add('active')") < tab_section.index(
        "ArchGraph.mount"
    )
    assert 'src="graph.js"' in INDEX_HTML
    # graph.css carries the pane, the grid and every animation; without the link
    # the tab renders unstyled boxes.
    assert 'href="graph.css"' in INDEX_HTML
    assert 'id="archFlow"' in INDEX_HTML


def test_graph_renders_dom_not_canvas():
    # v3 moved off <canvas> so stack names are real text: crisp at any zoom,
    # selectable in the details panel, and visible to the accessibility tree.
    assert "getContext" not in GRAPH_JS
    assert "createElementNS" in GRAPH_JS  # inline SVG edges
    assert "tabindex" in GRAPH_JS  # cards are focusable


def test_graph_has_no_animation_loop():
    # All motion is CSS keyframes/transitions, so idle CPU is zero and there is
    # no frame scheduler to leak on destroy().
    assert "requestAnimationFrame" not in GRAPH_JS
    css = (REPO / "dashboard" / "public" / "graph.css").read_text()
    assert "@keyframes" in css
    assert "prefers-reduced-motion" in css


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
