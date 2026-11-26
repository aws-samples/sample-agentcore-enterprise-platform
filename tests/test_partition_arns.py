"""IAM ARNs use the stack partition, not a hard-coded aws."""

from pathlib import Path

import aws_cdk as cdk

from stacks.gateway_stack import stack_partition

REPO = Path(__file__).resolve().parents[1]


def _stack(region: str | None = None) -> cdk.Stack:
    app = cdk.App()
    env = cdk.Environment(account="111122223333", region=region) if region else None
    return cdk.Stack(app, "S", env=env)


def test_china_region_uses_aws_cn():
    assert stack_partition(_stack("cn-north-1")) == "aws-cn"


def test_commercial_region_uses_aws():
    assert stack_partition(_stack("us-east-1")) == "aws"


def test_env_agnostic_stack_uses_the_partition_token():
    assert cdk.Token.is_unresolved(stack_partition(_stack()))


def test_gateway_stack_has_no_hard_coded_partition():
    assert "arn:aws:" not in (REPO / "stacks" / "gateway_stack.py").read_text()


def test_runtime_default_arns_use_the_partition():
    src = (REPO / "stacks" / "runtime_stack.py").read_text()
    for literal in ('"arn:aws:logs', '"arn:aws:bedrock:*', 'f"arn:aws:ssm'):
        assert literal not in src
