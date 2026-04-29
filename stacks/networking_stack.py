"""Networking Stack — Optional VPC with private subnets and VPC endpoints."""
import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_ssm as ssm
from constructs import Construct


class NetworkingStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, project_name: str, environment: str,
                 vpc_cidr: str = "10.0.0.0/16", enable_vpc_endpoints: bool = False, **kwargs):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"

        self.vpc = ec2.Vpc(self, "VPC",
            vpc_name=f"{prefix}-vpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24),
            ],
        )

        if enable_vpc_endpoints:
            self.vpc.add_interface_endpoint("BedrockEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            )
            self.vpc.add_gateway_endpoint("S3Endpoint",
                service=ec2.GatewayVpcEndpointAwsService.S3,
            )

        ssm.StringParameter(self, "SSMVpcId",
            parameter_name=f"/{project_name}/{environment}/networking/vpc-id",
            string_value=self.vpc.vpc_id,
        )

        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
