"""The Web Search connector target must ship whole: target + IAM + region gate.

Static guards in the style of test_agent_identity.py (the CI test job has no
aws_cdk/bedrock_agentcore, so these parse source). Three invariants, each of
which failed silently in some form elsewhere before:

1. The gateway role gets bedrock-agentcore:InvokeWebSearch on the service-owned
   tool ARN (account id "aws") — without it the connector target deploys fine
   and every search fails at call time.
2. The target sets the connector via add_property_override with the CFN
   PascalCase shape — the L1 mapping predates connector targets, so a plain
   target_configuration dict would be silently dropped (same trap as the
   Lambda key).
3. app.py region-gates the default — the connector exists in three regions,
   and creating the target elsewhere fails the whole gateway deploy.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_SRC = (ROOT / "stacks" / "gateway_stack.py").read_text()
APP_SRC = (ROOT / "app.py").read_text()


def test_gateway_role_gets_the_web_search_action():
    assert "bedrock-agentcore:InvokeWebSearch" in GATEWAY_SRC
    # The tool ARN is service-owned: literal "aws" where an account id would be.
    assert ":aws:tool/web-search.v1" in GATEWAY_SRC


def test_connector_is_set_via_property_override():
    # The L1 property mapping predates connector targets; the override path is
    # the only one that survives synth. If someone "cleans this up" into
    # target_configuration, the connector silently vanishes from the template.
    assert (
        'add_property_override(\n                "TargetConfiguration.Mcp.Connector"'
        in GATEWAY_SRC
    )
    assert '"ConnectorId": "web-search"' in GATEWAY_SRC


def test_no_version_pin_on_the_connector():
    # Dated pins rot (model-ID lesson). The default version tracks upstream.
    assert (
        '"Version"'
        not in GATEWAY_SRC.split('"ConnectorId": "web-search"')[1].split(")")[0]
    )


def test_app_region_gates_the_default():
    assert "WEB_SEARCH_REGIONS" in APP_SRC
    for region in ("us-east-1", "eu-west-1", "ap-northeast-1"):
        assert region in APP_SRC
    assert "enable_web_search=enable_web_search" in APP_SRC
