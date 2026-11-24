"""NetworkingStack must honour pinned AZs and keep max_azs=2 otherwise."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.networking_stack import NetworkingStack


@pytest.fixture(autouse=True)
def _offline_synth(monkeypatch):
    # Offline synth: allow the max_azs fallback instead of failing closed.
    monkeypatch.setenv("AGENTCORE_SKIP_AZ_PINNING", "1")


def _synth(**kwargs):
    # Env-agnostic stack: CDK does not compare pinned names to dummy AZs.
    app = cdk.App()
    stack = NetworkingStack(app, "net", project_name="p", environment="test", **kwargs)
    return stack, Template.from_stack(stack)


def test_pinned_availability_zones_are_used():
    pinned = ["us-east-1c", "us-east-1d"]
    stack, template = _synth(availability_zones=pinned)
    subnets = template.find_resources("AWS::EC2::Subnet")
    assert len(subnets) == 4
    for subnet in subnets.values():
        assert subnet["Properties"]["AvailabilityZone"] in pinned
    assert len(stack.private_subnet_ids) == 2


def test_default_still_builds_two_private_subnets():
    stack, template = _synth()
    assert len(stack.private_subnet_ids) == 2
    assert len(template.find_resources("AWS::EC2::Subnet")) == 4
