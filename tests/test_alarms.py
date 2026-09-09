"""observability.alarms: config plumbing and the live-verified metric facts.

The metric names and dimensions in the observability stack were read off a
live deployment — the AWS/Bedrock-AgentCore namespace is not documented well
enough to guess, and a wrong dimension is an alarm that can never fire. The
CI test job has no aws-cdk-lib, so the stack-side facts are pinned as source
drift, same approach as test_guardrail_enforcement.py.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.platform_config import PlatformConfig, to_env

OBS_STACK = (REPO / "stacks" / "observability_stack.py").read_text()
RUNTIME_STACK = (REPO / "stacks" / "runtime_stack.py").read_text()


# ── config ──


def test_default_is_off():
    config = PlatformConfig()
    assert config.observability.alarms is False
    assert config.observability.alarm_email == ""


def test_real_email_validates():
    config = PlatformConfig.model_validate(
        {"observability": {"alarms": True, "alarm_email": "ops@corpmail.io"}}
    )
    assert config.observability.alarm_email == "ops@corpmail.io"


@pytest.mark.parametrize(
    "email",
    [
        "user@example.com",  # trips the placeholder pattern — deliberately
        "REPLACE_ME@corp.io",
        "changeme@corp.io",
        "not-an-email",
        "a@b",  # no domain dot
        "two words@corp.io",
    ],
)
def test_placeholder_and_garbage_emails_rejected(email):
    with pytest.raises(ValidationError, match="email"):
        PlatformConfig.model_validate({"observability": {"alarm_email": email}})


def test_to_env_carries_flag_and_email():
    config = PlatformConfig.model_validate(
        {"observability": {"alarms": True, "alarm_email": "ops@corpmail.io"}}
    )
    env = to_env(config)
    assert env["ENABLE_ALARMS"] == "true"
    assert env["ALARM_EMAIL"] == "ops@corpmail.io"


def test_empty_email_is_dropped_from_env():
    env = to_env(PlatformConfig())
    assert env["ENABLE_ALARMS"] == "false"
    assert "ALARM_EMAIL" not in env


# ── preset ──


def test_platform_team_preset_enables_alarms():
    raw = yaml.safe_load((REPO / "presets" / "platform-team.yaml").read_text())
    config = PlatformConfig.model_validate(raw)
    assert config.observability.alarms is True


# ── stack source drift (live-verified facts) ──


def test_stack_uses_the_live_verified_namespaces():
    assert '"AWS/Bedrock-AgentCore"' in OBS_STACK
    # Model inference is a separate, account-level namespace.
    assert '"AWS/Bedrock"' in OBS_STACK


def test_missing_data_never_breaches():
    # Error metrics only emit when nonzero — missing data is good news, not
    # an alarm. Without this every quiet period would flap INSUFFICIENT_DATA.
    assert "TreatMissingData.NOT_BREACHING" in OBS_STACK


# The Name dimension on runtime metrics is the runtime's AgentCore name +
# "::DEFAULT". RuntimeStack and ObservabilityStack derive that name
# independently; this regex extracts the normalization idiom from both files
# so a rename on either side fails here instead of silently un-matching every
# runtime alarm's dimensions.
RT_NAME_RE = re.compile(
    r'f"\{project_name\}_\{environment\}_\{(\w+)\}"\.replace\("-", "_"\)'
)


def _normalized(match: re.Match) -> str:
    return match.group(0).replace(match.group(1), "x")


def test_runtime_name_dimension_matches_runtime_stack():
    obs = RT_NAME_RE.search(OBS_STACK)
    rt = RT_NAME_RE.search(RUNTIME_STACK)
    assert obs, "observability stack lost the rt_name derivation"
    assert rt, "runtime stack lost the rt_name derivation"
    assert _normalized(obs) == _normalized(rt)
    assert "::DEFAULT" in OBS_STACK, "runtime metrics need the endpoint suffix"
