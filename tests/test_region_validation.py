import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

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


def test_missing_zone_entry_warns_only_with_networking():
    # A stale table must not block a Region AgentCore has since added.
    findings = region_findings(
        _config(model="global.anthropic.claude-sonnet-4-6", web_search="off"),
        "eu-central-1",
    )
    assert [f.code for f in findings] == ["zones-unknown"]
    assert findings[0].level == "warning"
    assert "eu-central-1" in findings[0].message
    assert findings[0].render().startswith("[WARN]")

    off = region_findings(_config(networking=False), "eu-central-1")
    assert not [f for f in off if f.code == "zones-unknown"]


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


PRESET = Path(__file__).resolve().parents[1] / "presets" / "security-focused.yaml"


def _design(monkeypatch, capsys, region):
    from infra_utils import platform_config

    doc = yaml.safe_load(PRESET.read_text())
    doc["region"] = region
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "platform.yaml"
        path.write_text(yaml.safe_dump(doc))
        monkeypatch.setattr(sys, "argv", ["platform_config", "--design", str(path)])
        code = platform_config._main()
    return code, capsys.readouterr().out


def test_design_passes_and_prints_no_region_checks_in_a_supported_region(
    monkeypatch, capsys
):
    code, out = _design(monkeypatch, capsys, "us-east-1")
    assert code == 0
    assert "Region checks" not in out


def test_design_warns_but_passes_without_a_zone_entry(monkeypatch, capsys):
    # security-focused turns networking on; a missing zone table entry warns only.
    monkeypatch.delitem(runtime_network.SUPPORTED_ZONE_IDS, "us-east-1")
    code, out = _design(monkeypatch, capsys, "us-east-1")
    assert code == 0
    assert "[WARN]" in out and "us-east-1" in out
    assert "[ERROR]" not in out
