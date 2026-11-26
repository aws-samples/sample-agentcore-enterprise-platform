"""Security Stack — KMS CMK, IAM policy templates, CloudTrail."""

import aws_cdk as cdk
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_iam as iam
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
            trail_name = f"{prefix}-agentcore-trail"
            if retain_data and self.kms_key:
                # CloudTrail must be allowed to use the CMK that encrypts its
                # bucket, scoped to this trail. Per AWS docs:
                # https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-kms-key-policy-for-cloudtrail.html
                # The ARN is built from the name to avoid a circular reference.
                trail_arn = (
                    f"arn:{self.partition}:cloudtrail:{self.region}:"
                    f"{self.account}:trail/{trail_name}"
                )
                cloudtrail_principal = iam.ServicePrincipal("cloudtrail.amazonaws.com")
                self.kms_key.add_to_resource_policy(
                    iam.PolicyStatement(
                        sid="AllowCloudTrailEncryptLogs",
                        principals=[cloudtrail_principal],
                        actions=["kms:GenerateDataKey*"],
                        resources=["*"],
                        conditions={
                            "StringEquals": {"aws:SourceArn": trail_arn},
                            "StringLike": {
                                "kms:EncryptionContext:aws:cloudtrail:arn": (
                                    f"arn:{self.partition}:cloudtrail:*:"
                                    f"{self.account}:trail/*"
                                )
                            },
                        },
                    )
                )
                self.kms_key.add_to_resource_policy(
                    iam.PolicyStatement(
                        sid="AllowCloudTrailDescribeKey",
                        principals=[cloudtrail_principal],
                        actions=["kms:DescribeKey"],
                        resources=["*"],
                        conditions={"StringEquals": {"aws:SourceArn": trail_arn}},
                    )
                )
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
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                versioned=retain_data,
            )
            trail = cloudtrail.Trail(
                self,
                "AgentCoreTrail",
                trail_name=trail_name,
                # SSE-KMS for log files (KmsKeyId); without it CloudTrail
                # falls back to SSE-S3 regardless of the bucket default.
                encryption_key=self.kms_key if retain_data else None,
                bucket=trail_bucket,
                enable_file_validation=retain_data,
                is_multi_region_trail=retain_data,
            )
            if retain_data and self.kms_key:
                # Pin KmsKeyId on the L1 so the trail always uses SSE-KMS with
                # the CMK whose key policy grants CloudTrail above.
                trail.node.default_child.add_property_override(
                    "KmsKeyId", self.kms_key.key_arn
                )

        if self.kms_key:
            cdk.CfnOutput(self, "KmsKeyArn", value=self.kms_key.key_arn)
