"""Network configuration for AgentCore runtimes.

Deliberately free of CDK imports so it stays unit-testable without
aws-cdk-lib (see tests/test_runtime_network.py).

Two things this module exists to prevent, both found in the original code:
  - the wrong property shape. CfnRuntime wants NetworkModeConfig with
    Subnets/SecurityGroups; an invented key is accepted by synth and then
    ignored or rejected by CloudFormation.
  - a silent fallback to public networking. Asking for VPC mode without
    subnets used to render {"networkMode": "VPC"} and nothing else, so a
    "network isolated" deployment could come back on the public network.
"""

# AgentCore supports VPC connectivity only in specific Availability Zones, and
# an AZ *name* (us-east-1a) maps to a different AZ *id* (use1-az1) in every
# account — so a VPC that works in one account can fail in the next. Source:
# "VPC connectivity for Amazon Bedrock AgentCore Runtime and tools".
SUPPORTED_ZONE_IDS = {
    "us-east-1": ("use1-az1", "use1-az2", "use1-az4"),
    "us-east-2": ("use2-az1", "use2-az2", "use2-az3"),
    "us-west-2": ("usw2-az1", "usw2-az2", "usw2-az3"),
    "ap-south-1": ("aps1-az1", "aps1-az2", "aps1-az3"),
    "ap-northeast-1": ("apne1-az1", "apne1-az2", "apne1-az4"),
    "ap-northeast-2": ("apne2-az1", "apne2-az2", "apne2-az3"),
    "ap-southeast-1": ("apse1-az1", "apse1-az2", "apse1-az3"),
    "ap-southeast-2": ("apse2-az1", "apse2-az2", "apse2-az3"),
    "ap-southeast-5": ("apse5-az1", "apse5-az2", "apse5-az3"),
    "ap-southeast-7": ("apse7-az1", "apse7-az2", "apse7-az3"),
    "ca-central-1": ("cac1-az1", "cac1-az2", "cac1-az4"),
    "eu-central-1": ("euc1-az1", "euc1-az2", "euc1-az3"),
    "eu-north-1": ("eun1-az1", "eun1-az2", "eun1-az3"),
    "eu-south-1": ("eus1-az1", "eus1-az2", "eus1-az3"),
    "eu-south-2": ("eus2-az1", "eus2-az2", "eus2-az3"),
    "eu-west-1": ("euw1-az1", "euw1-az2", "euw1-az3"),
    "eu-west-2": ("euw2-az1", "euw2-az2", "euw2-az3"),
    "eu-west-3": ("euw3-az1", "euw3-az2", "euw3-az3"),
    "sa-east-1": ("sae1-az1", "sae1-az2", "sae1-az3"),
    "us-gov-west-1": ("usgw1-az1", "usgw1-az2", "usgw1-az3"),
}

PUBLIC = "PUBLIC"
VPC = "VPC"


def build_network_config(
    network_mode: str,
    subnet_ids: list[str] | None = None,
    security_group_ids: list[str] | None = None,
) -> dict:
    """CfnRuntime network_configuration for the requested mode.

    Raises ValueError for VPC mode without subnets or security groups, rather
    than quietly degrading to public networking.
    """
    if network_mode not in (PUBLIC, VPC):
        raise ValueError(
            f"network_mode must be {PUBLIC} or {VPC}, got {network_mode!r}"
        )
    if network_mode == PUBLIC:
        return {"networkMode": PUBLIC}

    if not subnet_ids or not security_group_ids:
        raise ValueError(
            "VPC network mode requires both subnet_ids and security_group_ids. "
            "Deploy the networking stack (enable_networking=true) so it can "
            "publish them, or use PUBLIC mode — silently falling back to "
            "public networking would contradict the deployment's own claim."
        )
    # Property names must match AWS::BedrockAgentCore::Runtime VpcConfig.
    return {
        "networkMode": VPC,
        "networkModeConfig": {
            "subnets": subnet_ids,
            "securityGroups": security_group_ids,
        },
    }


def unsupported_zone_ids(region: str, zone_ids: list[str]) -> list[str]:
    """Zone IDs AgentCore cannot place network interfaces in.

    An unknown region returns [] — a stale table should not block a deploy in
    a region AgentCore has since added.
    """
    supported = SUPPORTED_ZONE_IDS.get(region)
    if not supported:
        return []
    return [z for z in zone_ids if z not in supported]
