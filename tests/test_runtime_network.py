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
    SKIP_AZ_PINNING_ENV,
    SUPPORTED_ZONE_IDS,
    build_network_config,
    resolve_vpc_zones,
    select_supported_zone_names,
    unsupported_zone_ids,
    zone_name_to_id,
)

ACCOUNT = "111111111111"
UNSUPPORTED_FIRST = {
    "us-east-1a": "use1-az3",
    "us-east-1b": "use1-az5",
    "us-east-1c": "use1-az1",
    "us-east-1d": "use1-az4",
}


class FakeLookupError(Exception):
    pass


def _factory(zones, caller=ACCOUNT, fail=False, existing_subnet_zone_ids=None):
    calls = []

    class Sts:
        def get_caller_identity(self):
            if fail:
                raise FakeLookupError("no credentials")
            return {"Account": caller}

    class Ec2:
        def describe_availability_zones(self):
            return {
                "AvailabilityZones": [
                    {
                        "ZoneName": name,
                        "ZoneId": zone_id,
                        "State": "available",
                        "OptInStatus": "opt-in-not-required",
                    }
                    for name, zone_id in zones.items()
                ]
            }

        def describe_vpcs(self, **kwargs):
            if existing_subnet_zone_ids is None:
                return {"Vpcs": []}
            return {"Vpcs": [{"VpcId": "vpc-1"}]}

        def describe_subnets(self, **kwargs):
            return {
                "Subnets": [
                    {"AvailabilityZoneId": zone_id}
                    for zone_id in existing_subnet_zone_ids or []
                ]
            }

    def factory(service):
        calls.append(service)
        return Sts() if service == "sts" else Ec2()

    return factory, calls


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


def test_selection_skips_unsupported_first_zones():
    name_to_id = {
        "us-east-1a": "use1-az3",
        "us-east-1b": "use1-az5",
        "us-east-1c": "use1-az1",
        "us-east-1d": "use1-az4",
    }
    assert select_supported_zone_names("us-east-1", name_to_id) == [
        "us-east-1c",
        "us-east-1d",
    ]


def test_selection_unknown_region_keeps_default():
    assert select_supported_zone_names("me-central-1", {"x": "mec1-az1"}) is None


def test_selection_with_too_few_supported_zones_fails_clearly():
    name_to_id = {"us-east-1a": "use1-az1", "us-east-1b": "use1-az3"}
    with pytest.raises(ValueError) as excinfo:
        select_supported_zone_names("us-east-1", name_to_id)
    message = str(excinfo.value)
    for zone_id in SUPPORTED_ZONE_IDS["us-east-1"]:
        assert zone_id in message
    assert "use1-az3" in message
    assert "describe-availability-zones" in message


def test_zone_name_to_id_excludes_opt_in_and_unavailable_zones():
    class FakeEc2:
        def describe_availability_zones(self):
            return {
                "AvailabilityZones": [
                    {
                        "ZoneName": "us-east-1a",
                        "ZoneId": "use1-az1",
                        "State": "available",
                        "OptInStatus": "opt-in-not-required",
                    },
                    {
                        "ZoneName": "us-east-1-bos-1a",
                        "ZoneId": "use1-bos1-az1",
                        "State": "available",
                        "OptInStatus": "opted-in",
                    },
                    {
                        "ZoneName": "us-east-1b",
                        "ZoneId": "use1-az2",
                        "State": "impaired",
                        "OptInStatus": "opt-in-not-required",
                    },
                ]
            }

    assert zone_name_to_id(FakeEc2()) == {"us-east-1a": "use1-az1"}


def test_lookup_failure_fails_closed():
    factory, _ = _factory(UNSUPPORTED_FIRST, fail=True)
    with pytest.raises(RuntimeError, match=SKIP_AZ_PINNING_ENV):
        resolve_vpc_zones("us-east-1", ACCOUNT, factory, (FakeLookupError,), environ={})


def test_lookup_failure_falls_back_only_with_skip_flag():
    factory, _ = _factory(UNSUPPORTED_FIRST, fail=True)
    warnings = []
    result = resolve_vpc_zones(
        "us-east-1",
        ACCOUNT,
        factory,
        (FakeLookupError,),
        environ={SKIP_AZ_PINNING_ENV: "1"},
        warn=warnings.append,
    )
    assert result == (None, {})
    assert warnings and "WARNING" in warnings[0]


def test_account_mismatch_fails_before_az_lookup():
    factory, calls = _factory(UNSUPPORTED_FIRST, caller="222222222222")
    with pytest.raises(ValueError) as excinfo:
        resolve_vpc_zones("us-east-1", ACCOUNT, factory, (FakeLookupError,), environ={})
    assert "222222222222" in str(excinfo.value)
    assert ACCOUNT in str(excinfo.value)
    assert calls == ["sts"]


def test_supported_default_zones_keep_max_azs():
    zones = {
        "us-east-1a": "use1-az1",
        "us-east-1b": "use1-az2",
        "us-east-1c": "use1-az3",
    }
    factory, _ = _factory(zones)
    assert resolve_vpc_zones(
        "us-east-1", ACCOUNT, factory, (FakeLookupError,), environ={}
    ) == (None, zones)


def test_unsupported_default_zones_are_pinned():
    factory, _ = _factory(UNSUPPORTED_FIRST)
    zones, name_to_id = resolve_vpc_zones(
        "us-east-1", ACCOUNT, factory, (FakeLookupError,), environ={}
    )
    assert zones == ["us-east-1c", "us-east-1d"]
    assert name_to_id == UNSUPPORTED_FIRST


def test_skip_flag_name():
    assert SKIP_AZ_PINNING_ENV == "AGENTCORE_SKIP_AZ_PINNING"


def test_existing_vpc_in_unsupported_zones_warns_to_redeploy():
    factory, _ = _factory(
        UNSUPPORTED_FIRST, existing_subnet_zone_ids=["use1-az3", "use1-az5"]
    )
    warnings = []
    zones, _ = resolve_vpc_zones(
        "us-east-1",
        ACCOUNT,
        factory,
        (FakeLookupError,),
        environ={},
        warn=warnings.append,
        vpc_name="p-test-vpc",
    )
    assert zones == ["us-east-1c", "us-east-1d"]
    assert len(warnings) == 1
    for text in ("p-test-vpc", "use1-az3", "use1-az5", "Destroy", "TROUBLESHOOTING"):
        assert text in warnings[0]


def test_fresh_deployment_does_not_warn():
    factory, _ = _factory(UNSUPPORTED_FIRST)
    warnings = []
    resolve_vpc_zones(
        "us-east-1",
        ACCOUNT,
        factory,
        (FakeLookupError,),
        environ={},
        warn=warnings.append,
        vpc_name="p-test-vpc",
    )
    assert warnings == []


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
