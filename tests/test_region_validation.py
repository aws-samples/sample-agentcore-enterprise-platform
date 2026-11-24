from types import SimpleNamespace

import pytest

from infra_utils import runtime_network
from infra_utils.platform_config import (
    AgentsConfig,
    GatewayConfig,
    SecurityConfig,
    region_findings,
)

US_MODEL = "us.anthropic.claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def _zones(monkeypatch):
    monkeypatch.setattr(
        runtime_network,
        "SUPPORTED_ZONE_IDS",
        {"us-east-1": ("use1-az1", "use1-az2"), "eu-west-1": ("euw1-az1",)},
    )


def _config(model=US_MODEL, allowed=None, networking=True, web_search="on"):
    return SimpleNamespace(
        agents=AgentsConfig(model_id=model, allowed_models=allowed or []),
        gateway=GatewayConfig(web_search=web_search),
        security=SecurityConfig(networking=networking),
    )


def test_supported_region_has_no_findings():
    assert region_findings(_config(allowed=[US_MODEL]), "us-east-1") == []


def test_unsupported_zone_region_errors_only_with_networking():
    findings = region_findings(
        _config(model="global.anthropic.claude-sonnet-4-6", web_search="off"),
        "eu-central-1",
    )
    errors = [f for f in findings if f.level == "error"]
    assert [f.code for f in errors] == ["unsupported-zones"]
    assert "eu-central-1" in errors[0].message
    assert errors[0].render().startswith("[ERROR]")

    off = region_findings(_config(networking=False), "eu-central-1")
    assert not [f for f in off if f.code == "unsupported-zones"]


def test_unknown_model_warns_naming_region():
    findings = region_findings(
        _config(model="eu.acme.model-x-v1", web_search="off"), "eu-west-1"
    )
    assert [f.code for f in findings] == ["model-unknown"]
    assert findings[0].level == "warning"
    assert "eu-west-1" in findings[0].message
    assert findings[0].render().startswith("[WARN]")


def test_geo_mismatch_warns():
    findings = region_findings(_config(web_search="off"), "eu-west-1")
    assert [f.code for f in findings] == ["model-geo-mismatch"]
    assert "eu-west-1" in findings[0].message


def test_global_profile_allowed_everywhere():
    cfg = _config(model="global.anthropic.claude-sonnet-4-6", web_search="off")
    assert region_findings(cfg, "eu-west-1") == []


def test_web_search_outside_launch_regions_warns():
    findings = region_findings(
        _config(model="global.anthropic.claude-sonnet-4-6", networking=False),
        "us-west-2",
    )
    assert [f.code for f in findings] == ["web-search-region"]
    assert "us-west-2" in findings[0].message
    auto = _config(
        model="global.anthropic.claude-sonnet-4-6", networking=False, web_search="auto"
    )
    assert region_findings(auto, "us-west-2") == []


def test_unknown_region_warns_once_for_models():
    findings = region_findings(
        _config(networking=False, web_search="off", allowed=[US_MODEL]),
        "sa-east-1",
    )
    assert [f.code for f in findings] == ["model-region-unknown"]
    assert "sa-east-1" in findings[0].message


def test_region_findings_makes_no_aws_calls(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def boom(*_a, **_k):
        raise AssertionError("region_findings must not call AWS")

    monkeypatch.setattr(boto3, "client", boom)
    monkeypatch.setattr(boto3, "Session", boom)
    region_findings(_config(allowed=[US_MODEL]), "us-east-1")
    region_findings(_config(), "eu-central-1")
