"""Reusable AgentCore IAM Role construct."""
from aws_cdk import aws_iam as iam
from constructs import Construct


class AgentCoreRole(iam.Role):
    """IAM Role for AgentCore Runtime with standard permissions."""

    def __init__(self, scope: Construct, id: str, *, project_name: str, component_name: str,
                 extra_policy_statements: list[iam.PolicyStatement] | None = None, **kwargs):
        super().__init__(scope, id,
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            role_name=f"{project_name}-{component_name}-role",
            **kwargs)

        # Core permissions every AgentCore runtime needs
        self.add_to_policy(iam.PolicyStatement(
            actions=[
                "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetAuthorizationToken",
            ],
            resources=["*"],
        ))
        self.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
            ],
            resources=["arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/runtimes/*"],
        ))
        self.add_to_policy(iam.PolicyStatement(
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
            resources=["*"],
        ))
        self.add_to_policy(iam.PolicyStatement(
            actions=[
                "cloudwatch:PutMetricData",
            ],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
        ))
        self.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel", "bedrock:Converse", "bedrock:ConverseStream",
            ],
            resources=["arn:aws:bedrock:*::foundation-model/*"],
        ))
        # Least-privilege (item 2): scope SSM reads to this project's parameter path rather
        # than "*". See control-library/iam/runtime-execution-least-privilege.json for the
        # full reference policy (ECR/logs/bedrock scoping) teams can adopt.
        self.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:*:*:parameter/{project_name}/*"],
        ))

        for stmt in (extra_policy_statements or []):
            self.add_to_policy(stmt)
