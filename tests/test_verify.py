"""verify.py's check selection: driven by the contract, and complete.

Pure-Python (checks_for is deliberately a pure function; the AWS parts live
in main). The predecessor scripts/test.py could not fail — these tests pin
the properties that made replacing it worthwhile.
"""

import os
import subprocess  # nosec B404 — running our own verify scripts, no shell
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from verify import HEALTH_PROMPT, checks_for

from infra_utils.platform_config import PlatformConfig, discover_use_cases

USE_CASES = sorted(discover_use_cases())


def suffixes(**overrides) -> set[str]:
    config = PlatformConfig.model_validate(overrides)
    prefix = f"{config.project}-{config.environment}-"
    return {s.removeprefix(prefix) for s in config.expected_stacks()}


def names(checks) -> list[str]:
    return [n for n, _ in checks]


def test_default_footprint_gets_core_checks_only():
    got = names(checks_for(suffixes(), "orchestrator"))
    # identity first: every later check mints a token through that issuer.
    assert got == [
        "identity",
        "gateway",
        "memory",
        "observability",
        "orchestrator invoke",
    ]


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


def test_tool_consuming_patterns_require_a_successful_code_interpreter_result():
    checks = dict(checks_for(suffixes(), "strands-agent"))
    invoke = checks["orchestrator invoke"]
    assert invoke[invoke.index("--require-tool") + 1] == "execute_python_securely"
    assert "--require-tool-result" in invoke

    minimal = dict(checks_for(suffixes(), "orchestrator"))["orchestrator invoke"]
    assert "--require-tool" not in minimal


@pytest.mark.parametrize(
    "pattern", ["strands-agent", "agui-strands-agent", "langgraph-agent"]
)
def test_migration_uses_a_basic_live_invoke_not_pattern_specific_tools(pattern):
    invoke = dict(checks_for(suffixes(), pattern, migration=True))[
        "orchestrator invoke"
    ]

    assert invoke == ["invoke.py", "--require-success", HEALTH_PROMPT]
    assert "--require-tool" not in invoke
    assert "--agui" not in invoke


def test_migration_preserves_non_runtime_checks():
    got = names(
        checks_for(
            suffixes(security={"networking": True}),
            "strands-agent",
            require_guardrails=True,
            alarms=True,
            migration=True,
        )
    )

    assert got == [
        "identity",
        "gateway",
        "memory",
        "observability",
        "networking",
        "guardrail enforcement",
        "alarms",
        "orchestrator invoke",
    ]


def test_require_guardrails_selects_the_enforcement_check():
    on = names(checks_for(suffixes(), "orchestrator", require_guardrails=True))
    assert "guardrail enforcement" in on
    off = names(checks_for(suffixes(), "orchestrator"))
    assert "guardrail enforcement" not in off


def test_alarms_flag_selects_the_alarm_check():
    on = names(checks_for(suffixes(), "orchestrator", alarms=True))
    assert "alarms" in on
    off = names(checks_for(suffixes(), "orchestrator"))
    assert "alarms" not in off


def test_enabled_use_cases_are_verified_last():
    # A broken platform must fail on the platform check, not on the use case
    # riding on it — so use cases come after every core check, in order.
    got = names(
        checks_for(
            suffixes(use_cases={"hello-platform": {}}),
            "orchestrator",
            use_cases=["hello-platform"],
        )
    )
    assert got[-1] == "use case hello-platform"
    assert got[:-1] == [
        "identity",
        "gateway",
        "memory",
        "observability",
        "orchestrator invoke",
    ]


def test_disabled_use_cases_add_no_check():
    assert not [
        n for n in names(checks_for(suffixes(), "orchestrator")) if "use case" in n
    ]


def test_every_check_maps_to_an_existing_tool():
    # A selected check must never point at a script that does not exist —
    # that is exactly the silent-success class this command replaces.
    all_suffixes = suffixes(agents={"a2a": True}, security={"networking": True})
    for _, argv in checks_for(
        all_suffixes,
        "orchestrator",
        require_guardrails=True,
        alarms=True,
        use_cases=USE_CASES,
    ):
        assert (REPO / "scripts" / argv[0]).is_file(), argv[0]


def test_every_use_case_ships_a_verify():
    # CONTRIBUTING_USE_CASES.md: verify.py is REQUIRED, not a suggestion.
    for name in USE_CASES:
        assert (REPO / "use-cases" / name / "verify.py").is_file(), name


@pytest.mark.parametrize("name", USE_CASES)
def test_use_case_verify_fails_fast_without_a_deployment(name):
    # No credentials, no config, no metadata endpoint: the script must say
    # FAIL and exit 1 within seconds — never hang, never print a traceback.
    # The scrubbed env also keeps the laptop's own profiles (and any SSO
    # login prompt) out of the test.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": "us-east-1",
    }
    result = subprocess.run(  # nosec B603
        [sys.executable, str(REPO / "use-cases" / name / "verify.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL:" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_the_unfailable_health_check_stays_dead():
    assert not (REPO / "scripts" / "test.py").exists(), (
        "scripts/test.py is back — it printed success on broken deployments "
        "and had no non-zero exit path; use deploy.sh verify instead"
    )
