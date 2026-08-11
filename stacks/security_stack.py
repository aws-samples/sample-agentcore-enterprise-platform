"""Security Stack — KMS CMK, IAM policy templates, CloudTrail."""
import aws_cdk as cdk
from aws_cdk import aws_kms as kms, aws_cloudtrail as cloudtrail, aws_s3 as s3
from constructs import Construct


class SecurityStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, project_name: str, environment: str,
                 enable_kms: bool = True, enable_cloudtrail: bool = True, **kwargs):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"

        # KMS CMK for AgentCore Memory encryption
        self.kms_key = kms.Key(self, "AgentCoreKey",
            alias=f"alias/{prefix}-agentcore",
            description=f"CMK for {project_name} AgentCore resources",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        ) if enable_kms else None

        # CloudTrail for AgentCore API auditing
        if enable_cloudtrail:
            trail_bucket = s3.Bucket(self, "TrailBucket",
                bucket_name=f"{prefix}-cloudtrail-{self.account}",
                removal_policy=cdk.RemovalPolicy.DESTROY,
                auto_delete_objects=True,
                encryption=s3.BucketEncryption.S3_MANAGED,
            )
            cloudtrail.Trail(self, "AgentCoreTrail",
                trail_name=f"{prefix}-agentcore-trail",
                bucket=trail_bucket,
                is_multi_region_trail=False,
            )

        if self.kms_key:
            cdk.CfnOutput(self, "KmsKeyArn", value=self.kms_key.key_arn)
