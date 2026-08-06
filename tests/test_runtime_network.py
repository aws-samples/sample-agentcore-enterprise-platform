"""VPC network mode must render the shape CloudFormation expects, or fail loudly.

Regression guard for two defects found together: RuntimeStack emitted an
invented vpcConfiguration/subnetIds shape (CfnRuntime wants NetworkModeConfig
with Subnets/SecurityGroups), and app.py never passed a VPC at all — so
enable_networking=true built a VPC while every agent kept running on public
networking, under a README promising network isolation.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra_utils.runtime_network import (
    SUPPORTED_ZONE_IDS,
    build_network_config,
    unsupported_zone_ids,
)


def test_public_mode_is_unchanged():
    assert build_network_config("PUBLIC") == {"networkMode": "PUBLIC"}


def test_vpc_mode_uses_the_cfn_property_names():
    config = build_network_config("VPC", ["subnet-a", "subnet-b"], ["sg-a"])
    assert config == {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": ["subnet-a", "subnet-b"],
            "securityGroups": ["sg-a"],
        },
    }
    # The old invented keys must not come back: CloudFormation ignores them.
    assert "vpcConfiguration" not in config
    assert "subnetIds" not in str(config)
    assert "securityGroupIds" not in str(config)


@pytest.mark.parametrize(
    "subnets,groups",
    [(None, ["sg-a"]), ([], ["sg-a"]), (["subnet-a"], None), (["subnet-a"], [])],
)
def test_vpc_mode_without_a_vpc_fails_loudly(subnets, groups):
    """Never degrade to public networking behind the operator's back."""
    with pytest.raises(ValueError, match="VPC network mode requires"):
        build_network_config("VPC", subnets, groups)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="network_mode must be"):
        build_network_config("PRIVATE")


def test_unsupported_zones_are_reported():
    # us-east-1 supports use1-az1/az2/az4 only.
    assert unsupported_zone_ids("us-east-1", ["use1-az1", "use1-az4"]) == []
    assert unsupported_zone_ids("us-east-1", ["use1-az1", "use1-az3"]) == ["use1-az3"]


def test_unknown_region_does_not_block_a_deploy():
    """A stale table must not veto a region AgentCore has since added."""
    assert unsupported_zone_ids("me-central-1", ["mec1-az1"]) == []


def test_zone_id_table_is_well_formed():
    for region, zones in SUPPORTED_ZONE_IDS.items():
        assert len(zones) >= 3, region
        assert len(set(zones)) == len(zones), f"duplicate zone id in {region}"


def test_aws_facing_descriptions_are_ascii():
    """EC2 rejects non-ASCII in GroupDescription and friends.

    The repo uses em dashes freely in prose, and one reached a security group
    description — the stack rolled back mid-deploy. Cheap guard: any
    description= string in a stack must be ASCII.
    """
    import re

    pattern = re.compile(r'description\s*=\s*f?"([^"]*)"', re.IGNORECASE)
    offenders = []
    for path in sorted((Path(__file__).resolve().parents[1] / "stacks").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in pattern.finditer(line):
                if not match.group(1).isascii():
                    offenders.append(f"{path.name}:{lineno}: {match.group(1)}")
    assert not offenders, "non-ASCII in an AWS-facing description:\n" + "\n".join(
        offenders
    )
