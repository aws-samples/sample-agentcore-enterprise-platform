"""verify.py's check selection: driven by the contract, and complete.

Pure-Python (checks_for is deliberately a pure function; the AWS parts live
in main). The predecessor scripts/test.py could not fail — these tests pin
the properties that made replacing it worthwhile.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from verify import checks_for

from infra_utils.platform_config import PlatformConfig


def suffixes(**overrides) -> set[str]:
    config = PlatformConfig.model_validate(overrides)
    prefix = f"{config.project}-{config.environment}-"
    return {s.removeprefix(prefix) for s in config.expected_stacks()}


def names(checks) -> list[str]:
    return [n for n, _ in checks]


def test_default_footprint_gets_core_checks_only():
    got = names(checks_for(suffixes(), "orchestrator"))
    assert got == ["gateway", "memory", "observability", "orchestrator invoke"]


def test_a2a_footprint_adds_live_suba_invokes():
    got = names(checks_for(suffixes(agents={"a2a": True}), "orchestrator"))
    assert "a2a code-agent" in got
    assert "a2a research-agent" in got


def test_networking_footprint_checks_vpc_placement():
    got = names(checks_for(suffixes(security={"networking": True}), "orchestrator"))
    assert "networking" in got


def test_agui_patterns_invoke_over_agui():
    checks = dict(checks_for(suffixes(), "agui-strands-agent"))
    assert "--agui" in checks["orchestrator invoke"]
    checks = dict(checks_for(suffixes(), "orchestrator"))
    assert "--agui" not in checks["orchestrator invoke"]


def test_every_check_maps_to_an_existing_tool():
    # A selected check must never point at a script that does not exist —
    # that is exactly the silent-success class this command replaces.
    all_suffixes = suffixes(agents={"a2a": True}, security={"networking": True})
    for _, argv in checks_for(all_suffixes, "orchestrator"):
        assert (REPO / "scripts" / argv[0]).is_file(), argv[0]


def test_the_unfailable_health_check_stays_dead():
    assert not (REPO / "scripts" / "test.py").exists(), (
        "scripts/test.py is back — it printed success on broken deployments "
        "and had no non-zero exit path; use deploy.sh verify instead"
    )
