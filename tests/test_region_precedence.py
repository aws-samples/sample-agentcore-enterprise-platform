"""The manifest region wins over CDK_DEFAULT_REGION; env only fills a gap."""

from pathlib import Path

from infra_utils.platform_config import resolve_synth_region

REPO = Path(__file__).resolve().parents[1]


def test_manifest_region_beats_cdk_default_region():
    assert (
        resolve_synth_region(manifest_region="eu-west-1", env_region="us-west-2")
        == "eu-west-1"
    )


def test_cdk_default_region_fills_a_missing_value():
    assert resolve_synth_region(env_region="us-west-2") == "us-west-2"


def test_context_region_wins():
    assert (
        resolve_synth_region("ap-northeast-1", "eu-west-1", "us-west-2")
        == "ap-northeast-1"
    )


def test_default_region():
    assert resolve_synth_region() == "us-east-1"


def test_app_py_uses_the_helper():
    src = (REPO / "app.py").read_text()
    assert "region = resolve_synth_region(" in src
    assert 'or os.environ.get("CDK_DEFAULT_REGION", "")' not in src
