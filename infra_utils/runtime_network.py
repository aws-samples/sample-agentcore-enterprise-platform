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

import os

# Set to 1 only for offline synths: a failed AZ lookup then keeps max_azs=2
# instead of failing the synth.
SKIP_AZ_PINNING_ENV = "AGENTCORE_SKIP_AZ_PINNING"

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


def select_supported_zone_names(
    region: str, name_to_id: dict[str, str], count: int = 2
) -> list[str] | None:
    """The first ``count`` AZ names (sorted) whose zone id AgentCore supports.

    ``name_to_id`` is this account's AZ name -> AZ id mapping. An unknown
    region returns None, meaning "keep CDK's default AZ choice": the same
    stale-table policy as unsupported_zone_ids. Raises ValueError when the
    account offers fewer than ``count`` supported zones.
    """
    supported = SUPPORTED_ZONE_IDS.get(region)
    if not supported:
        return None
    chosen = [name for name in sorted(name_to_id) if name_to_id[name] in supported]
    if len(chosen) < count:
        offered = sorted(set(name_to_id.values()))
        raise ValueError(
            f"AgentCore VPC mode in {region} supports only zone ids "
            f"{', '.join(supported)}, but this account offers "
            f"{', '.join(offered) or 'none'}; {count} supported zones are "
            "needed. Check the mapping with "
            "`aws ec2 describe-availability-zones --region "
            f"{region}`."
        )
    return chosen[:count]


def default_zones_supported(
    region: str, name_to_id: dict[str, str], count: int = 2
) -> bool:
    """Whether CDK's default pick (first ``count`` sorted names) is supported.

    An unknown region counts as supported: keep today's behaviour.
    """
    supported = SUPPORTED_ZONE_IDS.get(region)
    if not supported:
        return True
    return all(name_to_id[name] in supported for name in sorted(name_to_id)[:count])


def resolve_vpc_zones(
    region: str,
    target_account: str,
    client_factory,
    lookup_errors: tuple,
    *,
    environ=None,
    warn=print,
    count: int = 2,
    vpc_name: str = "",
) -> tuple[list[str] | None, dict[str, str]]:
    """AZ names to pin the VPC to, plus the account's name -> id mapping.

    Returns (None, ...) when CDK's default max_azs choice should be kept: an
    unknown region, or default zones that are already supported (so existing
    deployments synthesize an identical template). The caller account must
    match ``target_account``, otherwise the mapping would be another
    account's. A failed lookup raises unless AGENTCORE_SKIP_AZ_PINNING=1.
    When pinning applies and an existing VPC named ``vpc_name`` already has
    subnets in unsupported zone ids, a warning tells the operator to destroy
    and redeploy the networking stack (no automatic migration).
    """
    if region not in SUPPORTED_ZONE_IDS:
        return None, {}
    environ = os.environ if environ is None else environ
    skip = environ.get(SKIP_AZ_PINNING_ENV) == "1"

    def lookup(what, call):
        try:
            return call()
        except lookup_errors as exc:
            if not skip:
                raise RuntimeError(
                    f"Could not {what} to choose AgentCore-supported AZs in "
                    f"{region} ({exc}). Fix the AWS credentials or endpoint, "
                    f"or set {SKIP_AZ_PINNING_ENV}=1 to keep max_azs=2 "
                    "(offline synth only)."
                ) from exc
            warn(
                f"WARNING: AZ pinning skipped ({exc}); {SKIP_AZ_PINNING_ENV}=1 "
                "keeps max_azs=2. scripts/check_network.py verifies placement "
                "after deploy."
            )
            return None

    if target_account:
        identity = lookup(
            "read the caller account (sts get-caller-identity)",
            lambda: client_factory("sts").get_caller_identity(),
        )
        if identity is None:
            return None, {}
        caller = identity["Account"]
        if caller != target_account:
            raise ValueError(
                f"AWS credentials belong to account {caller}, but the target "
                f"account (CDK_DEFAULT_ACCOUNT) is {target_account}. AZ name -> "
                "id mappings differ per account; use credentials for the "
                "target account."
            )
    name_to_id = lookup(
        "list availability zones (ec2 describe-availability-zones)",
        lambda: zone_name_to_id(client_factory("ec2")),
    )
    if name_to_id is None:
        return None, {}
    if default_zones_supported(region, name_to_id, count):
        return None, name_to_id
    chosen = select_supported_zone_names(region, name_to_id, count)
    if vpc_name:
        stale = lookup(
            "inspect the existing VPC subnets (ec2 describe-subnets)",
            lambda: existing_unsupported_zone_ids(
                client_factory("ec2"), region, vpc_name
            ),
        )
        if stale:
            warn(
                f"WARNING: existing VPC {vpc_name} has subnets in zone ids "
                f"{', '.join(stale)}, which AgentCore VPC mode does not "
                f"support. This synth moves the subnets to {', '.join(chosen)}; "
                "CloudFormation cannot move them in place. Destroy the "
                "networking stack (after any failed runtime/gateway stacks "
                "that import its subnets) and deploy it again. It holds no "
                "data and the runtime never started. See "
                "docs/TROUBLESHOOTING.md. There is no automatic migration."
            )
    return chosen, name_to_id


def existing_unsupported_zone_ids(ec2_client, region: str, vpc_name: str) -> list[str]:
    """Unsupported zone ids used by subnets of the VPC tagged ``vpc_name``.

    Returns [] when no such VPC exists (a fresh deployment).
    """
    vpcs = ec2_client.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [vpc_name]}]
    ).get("Vpcs", [])
    if not vpcs:
        return []
    subnets = ec2_client.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [v["VpcId"] for v in vpcs]}]
    ).get("Subnets", [])
    zone_ids = [s["AvailabilityZoneId"] for s in subnets]
    return sorted(set(unsupported_zone_ids(region, zone_ids)))


def zone_name_to_id(ec2_client) -> dict[str, str]:
    """AZ name -> AZ id for available, non-opt-in zones of ``ec2_client``."""
    response = ec2_client.describe_availability_zones()
    return {
        zone["ZoneName"]: zone["ZoneId"]
        for zone in response.get("AvailabilityZones", [])
        if zone.get("State") == "available"
        and zone.get("OptInStatus") == "opt-in-not-required"
    }
