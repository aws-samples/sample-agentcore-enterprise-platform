"""Memory Stack — AgentCore Memory with configurable strategies.

Implements Requirement 8: AgentCore Memory Configuration
- AgentCore Memory resource with semantic and user_preference strategies
- Optional KMS CMK encryption for data at rest
- SSM Parameters for cross-stack consumption
"""
import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

from infra_utils.policy_loader import load_control_json


class MemoryStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        kms_key_arn: str = "",
        event_expiry_days: int = 30,
        use_long_term_memory: bool = False,
        ltm_top_k: int = 10,
        ltm_relevance_score: float = 0.3,
        enable_resource_policies: bool = False,
        org_id: str = "",
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        name = f"{project_name}_{environment}_memory".replace("-", "_")

        # ── Memory Strategies (typed property objects) ──
        strategies = [
            agentcore.CfnMemory.MemoryStrategyProperty(
                user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                    name=f"{name}_user_pref",
                    description="User preference tracking strategy",
                    namespaces=["USER_ID"],
                ),
            ),
        ]

        # Long-term memory (semantic fact extraction) — optional, incurs additional cost
        if use_long_term_memory:
            strategies.insert(0, agentcore.CfnMemory.MemoryStrategyProperty(
                semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                    name=f"{name}_semantic",
                    description="Semantic fact extraction and override strategy",
                    namespaces=["AGENT_ID"],
                ),
            ))

        # ── CfnMemory ──
        props: dict = {
            "name": name,
            "description": f"Workshop memory for {project_name}/{environment}",
            "event_expiry_duration": event_expiry_days,
            "memory_strategies": strategies,
        }
        if kms_key_arn:
            props["encryption_key_arn"] = kms_key_arn

        self.memory = agentcore.CfnMemory(self, "Memory", **props)

        # ── Resource-based policy (optional, control-library) ──
        # Attaches an "in-account-only" resource policy to the Memory resource so principals
        # outside this AWS Organization cannot call the memory data-plane APIs directly.
        # AgentCore Memory has no L1/CFN property for resource policies, so we call the
        # PutResourcePolicy control-plane API via a custom resource.
        if enable_resource_policies:
            if not org_id:
                raise ValueError(
                    "MemoryStack: enable_resource_policies=True requires org_id "
                    "(pass -c org_id=o-xxxx or set ORG_ID) so the aws:PrincipalOrgID "
                    "deny guard can render."
                )
            policy_json = load_control_json(
                "resource-policy.memory.in-account-only",
                {
                    "account_id": self.account,
                    "memory_arn": self.memory.attr_memory_arn,
                    "org_id": org_id,
                },
            )
            # NOTE: the AWS SDK service/action identifiers below must be confirmed on first
            # `cdk synth`/deploy against the installed SDK version. boto3 exposes this as
            # bedrock-agentcore-control.put_resource_policy; the JS SDK client used by
            # AwsCustomResource is @aws-sdk/client-bedrock-agentcore-control.
            sdk_service = "bedrock-agentcore-control"
            cr.AwsCustomResource(
                self,
                "MemoryResourcePolicy",
                on_create=cr.AwsSdkCall(
                    service=sdk_service,
                    action="putResourcePolicy",
                    parameters={
                        "resourceArn": self.memory.attr_memory_arn,
                        "policy": policy_json,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(f"{name}-resource-policy"),
                ),
                on_update=cr.AwsSdkCall(
                    service=sdk_service,
                    action="putResourcePolicy",
                    parameters={
                        "resourceArn": self.memory.attr_memory_arn,
                        "policy": policy_json,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(f"{name}-resource-policy"),
                ),
                on_delete=cr.AwsSdkCall(
                    service=sdk_service,
                    action="deleteResourcePolicy",
                    parameters={"resourceArn": self.memory.attr_memory_arn},
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([
                    iam.PolicyStatement(
                        actions=[
                            "bedrock-agentcore:PutResourcePolicy",
                            "bedrock-agentcore:DeleteResourcePolicy",
                            "bedrock-agentcore:GetResourcePolicy",
                        ],
                        resources=["*"],
                    ),
                ]),
                install_latest_aws_sdk=True,
            )

        # ── SSM Parameters ──
        ssm.StringParameter(self, "SSMMemoryId",
            parameter_name=f"/{project_name}/{environment}/memory/memory-id",
            string_value=self.memory.attr_memory_id,
            description=f"AgentCore Memory ID for {project_name}/{environment}",
        )
        ssm.StringParameter(self, "SSMMemoryArn",
            parameter_name=f"/{project_name}/{environment}/memory/memory-arn",
            string_value=self.memory.attr_memory_arn,
            description=f"AgentCore Memory ARN for {project_name}/{environment}",
        )

        # ── Outputs ──
        cdk.CfnOutput(self, "MemoryId", value=self.memory.attr_memory_id,
                       export_name=f"{project_name}-{environment}-memory-id")
        cdk.CfnOutput(self, "MemoryArn", value=self.memory.attr_memory_arn,
                       export_name=f"{project_name}-{environment}-memory-arn")

    @property
    def memory_id(self) -> str:
        return self.memory.attr_memory_id

    @property
    def memory_arn(self) -> str:
        return self.memory.attr_memory_arn
