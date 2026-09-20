"""Security Stack — KMS CMK, IAM policy templates, CloudTrail."""

import aws_cdk as cdk
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct


class SecurityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        enable_kms: bool = True,
        enable_cloudtrail: bool = True,
        retain_data: bool = False,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        lifecycle_policy = (
            cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY
        )

        # KMS CMK for AgentCore Memory encryption
        self.kms_key = (
            kms.Key(
                self,
                "AgentCoreKey",
                alias=f"alias/{prefix}-agentcore",
                description=f"CMK for {project_name} AgentCore resources",
                enable_key_rotation=True,
                removal_policy=lifecycle_policy,
            )
            if enable_kms
            else None
        )

        # CloudTrail for AgentCore API auditing
        if enable_cloudtrail:
            trail_bucket = s3.Bucket(
                self,
                "TrailBucket",
                bucket_name=f"{prefix}-cloudtrail-{self.account}",
                removal_policy=lifecycle_policy,
                auto_delete_objects=not retain_data,
                encryption=(
                    s3.BucketEncryption.KMS
                    if retain_data and self.kms_key
                    else s3.BucketEncryption.S3_MANAGED
                ),
                encryption_key=self.kms_key if retain_data else None,
                enforce_ssl=True,
                versioned=retain_data,
            )
            cloudtrail.Trail(
                self,
                "AgentCoreTrail",
                trail_name=f"{prefix}-agentcore-trail",
                bucket=trail_bucket,
                enable_file_validation=retain_data,
                is_multi_region_trail=retain_data,
            )

        if self.kms_key:
            cdk.CfnOutput(self, "KmsKeyArn", value=self.kms_key.key_arn)
