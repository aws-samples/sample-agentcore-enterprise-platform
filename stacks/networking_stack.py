"""Networking Stack — Optional VPC with private subnets and VPC endpoints."""

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra_utils.policy_loader import load_control


class NetworkingStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        vpc_cidr: str = "10.0.0.0/16",
        enable_vpc_endpoints: bool = False,
        org_id: str = "",
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"

        self.vpc = ec2.Vpc(
            self,
            "VPC",
            vpc_name=f"{prefix}-vpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # Security group for the AgentCore runtime ENIs. AgentCore creates the
        # interfaces in the private subnets; this group is what decides where the
        # agent can talk to. Egress is limited to 443 because every dependency
        # (Bedrock, AgentCore, Secrets Manager, ECR, CloudWatch Logs) is HTTPS.
        # No inbound rules: the runtime only makes outbound connections, and
        # callers reach it through the AgentCore data plane, not the VPC.
        self.runtime_security_group = ec2.SecurityGroup(
            self,
            "RuntimeSecurityGroup",
            vpc=self.vpc,
            security_group_name=f"{prefix}-runtime-sg",
            # ASCII only: EC2 rejects a GroupDescription containing anything
            # else, so the em dashes used freely in comments cannot appear here.
            description="AgentCore runtime ENIs - HTTPS egress only",
            allow_all_outbound=False,
        )
        self.runtime_security_group.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS to AWS service endpoints and NAT egress",
        )

        if enable_vpc_endpoints:
            self.vpc.add_interface_endpoint(
                "BedrockEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            )
            # Required for VPC-mode container runtimes: AgentCore pulls and
            # periodically refreshes the image from ECR, whose layers live in S3.
            # Without these the traffic either fails (no NAT) or bills as NAT
            # data processing (with NAT) — the S3 gateway endpoint is free.
            self.vpc.add_interface_endpoint(
                "EcrApiEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.ECR,
            )
            self.vpc.add_interface_endpoint(
                "EcrDockerEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            )
            self.vpc.add_interface_endpoint(
                "LogsEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            )
            self.vpc.add_gateway_endpoint(
                "S3Endpoint",
                service=ec2.GatewayVpcEndpointAwsService.S3,
            )

            # AgentCore Gateway interface endpoint with a fine-grained, org-scoped endpoint
            # policy (item 2). Only principals in the org can invoke AgentCore through it.
            agentcore_endpoint = self.vpc.add_interface_endpoint(
                "AgentCoreGatewayEndpoint",
                service=ec2.InterfaceVpcEndpointService(
                    f"com.amazonaws.{self.region}.bedrock-agentcore.gateway", 443
                ),
                private_dns_enabled=True,
            )
            if org_id:
                endpoint_policy = load_control(
                    "vpce.agentcore-in-org", {"org_id": org_id}
                )
                agentcore_endpoint.node.default_child.add_property_override(
                    "PolicyDocument", endpoint_policy
                )

        # Private subnets are the only valid placement for AgentCore ENIs: a
        # public subnet gives them no internet route (AWS docs), so the runtimes
        # would lose Bedrock access.
        self.private_subnet_ids = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        ).subnet_ids

        ssm.StringParameter(
            self,
            "SSMVpcId",
            parameter_name=f"/{project_name}/{environment}/networking/vpc-id",
            string_value=self.vpc.vpc_id,
        )
        # Published so scripts and the module C verify can check placement
        # without parsing CloudFormation outputs.
        ssm.StringParameter(
            self,
            "SSMPrivateSubnetIds",
            parameter_name=f"/{project_name}/{environment}/networking/private-subnet-ids",
            string_value=",".join(self.private_subnet_ids),
        )
        ssm.StringParameter(
            self,
            "SSMRuntimeSecurityGroupId",
            parameter_name=f"/{project_name}/{environment}/networking/runtime-security-group-id",
            string_value=self.runtime_security_group.security_group_id,
        )

        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        cdk.CfnOutput(self, "PrivateSubnetIds", value=",".join(self.private_subnet_ids))
        cdk.CfnOutput(
            self,
            "RuntimeSecurityGroupId",
            value=self.runtime_security_group.security_group_id,
        )
