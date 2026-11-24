"""The CloudFormation execution role for the software factory's deploys.

``software-factory-cdk-exec`` is the role CloudFormation acts as while it applies a change set
to the factory stacks. Only ``cloudformation.amazonaws.com`` in this account can assume it, so
its permissions are reachable only by an operator who may pass it to CloudFormation.

Nothing outside the account is trusted: there is no GitHub OIDC provider and no role that a
CI system can assume. Deploys run from an operator workstation.
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
)
from constructs import Construct

# The one managed policy the factory stacks attach to a role. Restricting
# iam:AttachRolePolicy to this ARN is what stops the exec role from attaching
# AdministratorAccess to a role it just created.
ALLOWED_MANAGED_POLICIES = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
)


class SoftwareFactoryCicdStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bootstrap_qualifier: str = "hnb659fds",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.assets_bucket = (
            f"cdk-{bootstrap_qualifier}-assets-{self.account}-{self.region}"
        )
        self.bootstrap_version_param = (
            f"arn:aws:ssm:{self.region}:{self.account}:parameter"
            f"/cdk-bootstrap/{bootstrap_qualifier}/version"
        )
        self.exec_role = self._exec_role()
        cdk.CfnOutput(self, "CfnExecRoleArn", value=self.exec_role.role_arn)

    # ------------------------------------------------- CloudFormation's own role

    def _exec_role(self) -> iam.Role:
        """What CloudFormation may do while applying a change set to the factory stacks.

        Broad per service, but every resource is name-scoped to the factory, and the only
        caller is CloudFormation, on behalf of an operator allowed to pass this role.
        """
        role = iam.Role(
            self,
            "CdkExec",
            role_name="software-factory-cdk-exec",
            assumed_by=iam.ServicePrincipal(
                "cloudformation.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="CloudFormation execution role for the two factory stacks",
        )
        for actions, resources in self._exec_statements():
            role.add_to_policy(
                iam.PolicyStatement(actions=actions, resources=resources)
            )
        # Attaching a managed policy is the one escalation primitive left, so the policy ARN
        # is allow-listed rather than the role name alone.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                resources=self._named("iam", "role"),
                conditions={
                    "ArnEquals": {"iam:PolicyARN": list(ALLOWED_MANAGED_POLICIES)}
                },
            )
        )
        # An IAM role may only be handed to the services the factory actually runs on.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=self._named("iam", "role"),
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": [
                            "lambda.amazonaws.com",
                            "codebuild.amazonaws.com",
                            "events.amazonaws.com",
                            "bedrock-agentcore.amazonaws.com",
                        ]
                    }
                },
            )
        )
        return role

    def _named(self, service: str, kind: str) -> list[str]:
        """ARNs for the factory's own resources, by the two name prefixes it uses.

        Everything in both stacks is either named ``software-factory-*`` (or
        ``software_factory*`` where the service forbids a hyphen) or auto-named by CDK, which
        prefixes the stack name -- hence ``SoftwareFactory*``.
        """
        region = "" if service == "iam" else self.region
        # Lambda puts a colon after "function"; IAM a slash after "role".
        sep = ":" if service == "lambda" else "/"
        base = f"arn:aws:{service}:{region}:{self.account}:{kind}{sep}"
        return [
            f"{base}software-factory-*",
            f"{base}software_factory*",
            f"{base}SoftwareFactory*",
        ]

    def _exec_statements(self) -> list[tuple[list[str], list[str]]]:
        acct, region = self.account, self.region
        return [
            # --- IAM: the stacks' 8 roles and their inline policies.
            (
                [
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:GetRole",
                    "iam:UpdateRole",
                    "iam:UpdateRoleDescription",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:PutRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:GetRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:ListAttachedRolePolicies",
                    "iam:TagRole",
                    "iam:UntagRole",
                    "iam:ListRoleTags",
                ],
                self._named("iam", "role"),
            ),
            # --- DynamoDB: the software-factory-* tables. Creating a table also reads its
            # contributor insights and resource policy. DeleteTable is left out on purpose.
            (
                [
                    "dynamodb:CreateTable",
                    "dynamodb:DescribeContributorInsights",
                    "dynamodb:GetResourcePolicy",
                    "dynamodb:UpdateTable",
                    "dynamodb:DescribeTable",
                    "dynamodb:DescribeTimeToLive",
                    "dynamodb:UpdateTimeToLive",
                    "dynamodb:DescribeContinuousBackups",
                    "dynamodb:UpdateContinuousBackups",
                    "dynamodb:DescribeTableReplicaAutoScaling",
                    "dynamodb:UpdateTableReplicaAutoScaling",
                    "dynamodb:TagResource",
                    "dynamodb:UntagResource",
                    "dynamodb:ListTagsOfResource",
                    "dynamodb:DescribeKinesisStreamingDestination",
                    # The runs table's stream (resource table/<name>/stream/*).
                    "dynamodb:DescribeStream",
                ],
                [f"arn:aws:dynamodb:{region}:{acct}:table/software-factory-*"],
            ),
            # --- Lambda: ingress, dispatcher, redrive, image-build trigger, CR provider.
            (
                [
                    "lambda:CreateFunction",
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                    "lambda:GetFunctionConfiguration",
                    # Read by CloudFormation's update handler on every function update.
                    "lambda:GetFunctionCodeSigningConfig",
                    "lambda:GetFunctionRecursionConfig",
                    "lambda:GetFunctionScalingConfig",
                    "lambda:GetRuntimeManagementConfig",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:AddPermission",
                    "lambda:RemovePermission",
                    "lambda:GetPolicy",
                    "lambda:TagResource",
                    "lambda:UntagResource",
                    "lambda:ListTags",
                    "lambda:PutFunctionConcurrency",
                    "lambda:DeleteFunctionConcurrency",
                    "lambda:CreateEventSourceMapping",
                    "lambda:DeleteEventSourceMapping",
                    "lambda:GetEventSourceMapping",
                    "lambda:UpdateEventSourceMapping",
                    "lambda:InvokeFunction",
                ],
                self._named("lambda", "function")
                # Event source mappings carry a UUID, not a name.
                + [f"arn:aws:lambda:{region}:{acct}:event-source-mapping:*"],
            ),
            # --- SQS: the FIFO work queue and its parking lot.
            (
                [
                    "sqs:CreateQueue",
                    "sqs:DeleteQueue",
                    "sqs:GetQueueAttributes",
                    "sqs:SetQueueAttributes",
                    "sqs:GetQueueUrl",
                    "sqs:TagQueue",
                    "sqs:UntagQueue",
                    "sqs:ListQueueTags",
                ],
                [f"arn:aws:sqs:{region}:{acct}:software-factory-*"],
            ),
            # --- SNS: budget and alarm notifications.
            (
                [
                    "sns:CreateTopic",
                    "sns:DeleteTopic",
                    "sns:GetTopicAttributes",
                    "sns:SetTopicAttributes",
                    "sns:Subscribe",
                    "sns:Unsubscribe",
                    "sns:TagResource",
                    "sns:UntagResource",
                    "sns:ListTagsForResource",
                ],
                [f"arn:aws:sns:{region}:{acct}:software-factory-*"],
            ),
            # --- Secrets Manager + SSM: the webhook HMAC secret, the App secret, installation id.
            (
                [
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:UpdateSecret",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:GetRandomPassword",
                    "secretsmanager:TagResource",
                    "secretsmanager:UntagResource",
                ],
                [f"arn:aws:secretsmanager:{region}:{acct}:secret:software-factory/*"],
            ),
            (
                [
                    "ssm:PutParameter",
                    "ssm:DeleteParameter",
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:AddTagsToResource",
                    "ssm:RemoveTagsFromResource",
                    "ssm:ListTagsForResource",
                ],
                [
                    f"arn:aws:ssm:{region}:{acct}:parameter/software-factory/*",
                    self.bootstrap_version_param,
                ],
            ),
            # --- ECR + CodeBuild: the ARM64 runtime image build.
            (
                [
                    "ecr:CreateRepository",
                    "ecr:DeleteRepository",
                    "ecr:DescribeRepositories",
                    "ecr:PutLifecyclePolicy",
                    "ecr:DeleteLifecyclePolicy",
                    "ecr:GetLifecyclePolicy",
                    "ecr:SetRepositoryPolicy",
                    "ecr:DeleteRepositoryPolicy",
                    "ecr:GetRepositoryPolicy",
                    "ecr:TagResource",
                    "ecr:UntagResource",
                    "ecr:ListTagsForResource",
                    "ecr:PutImageScanningConfiguration",
                    "ecr:PutImageTagMutability",
                ],
                [f"arn:aws:ecr:{region}:{acct}:repository/software-factory*"],
            ),
            (
                [
                    "codebuild:CreateProject",
                    "codebuild:DeleteProject",
                    "codebuild:UpdateProject",
                    "codebuild:BatchGetProjects",
                    "codebuild:StartBuild",
                    "codebuild:BatchGetBuilds",
                ],
                [f"arn:aws:codebuild:{region}:{acct}:project/software-factory*"],
            ),
            # --- CloudWatch Logs: the seven log groups.
            (
                [
                    "logs:CreateLogGroup",
                    "logs:DeleteLogGroup",
                    "logs:DescribeLogGroups",
                    # Read by CloudFormation's log-group handler after it creates one.
                    "logs:DescribeIndexPolicies",
                    "logs:GetDataProtectionPolicy",
                    "logs:PutRetentionPolicy",
                    "logs:DeleteRetentionPolicy",
                    "logs:TagResource",
                    "logs:UntagResource",
                    "logs:ListTagsForResource",
                ],
                [
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/lambda/software-factory-*",
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/lambda/SoftwareFactory*",
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/codebuild/software-factory*",
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/bedrock-agentcore/runtimes/software_factory*",
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/vendedlogs/software-factory*",
                    f"arn:aws:logs:{region}:{acct}:log-group:software-factory*",
                ],
            ),
            # --- EventBridge: the four schedules.
            (
                [
                    "events:PutRule",
                    "events:DeleteRule",
                    "events:DescribeRule",
                    "events:PutTargets",
                    "events:RemoveTargets",
                    "events:ListTargetsByRule",
                    "events:TagResource",
                    "events:UntagResource",
                ],
                [f"arn:aws:events:{region}:{acct}:rule/software-factory-*"],
            ),
            # --- AgentCore runtime `software_factory`.
            (
                [
                    "bedrock-agentcore:CreateAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                    # An update moves the runtime's DEFAULT endpoint to the new version.
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                    "bedrock-agentcore:TagResource",
                    "bedrock-agentcore:UntagResource",
                    "bedrock-agentcore:ListTagsForResource",
                ],
                [
                    f"arn:aws:bedrock-agentcore:{region}:{acct}:runtime/software_factory*"
                ],
            ),
            # --- AgentCore workload identity the Asana sync runs as.
            (
                [
                    "bedrock-agentcore:CreateWorkloadIdentity",
                    "bedrock-agentcore:GetWorkloadIdentity",
                    "bedrock-agentcore:UpdateWorkloadIdentity",
                    "bedrock-agentcore:DeleteWorkloadIdentity",
                    "bedrock-agentcore:TagResource",
                    "bedrock-agentcore:UntagResource",
                    "bedrock-agentcore:ListTagsForResource",
                ],
                [
                    f"arn:aws:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default/workload-identity/software-factory-*",
                ],
            ),
            # --- Budget. Budgets ARNs carry no region.
            (
                [
                    "budgets:CreateBudget",
                    "budgets:ModifyBudget",
                    "budgets:DeleteBudget",
                    "budgets:DescribeBudget",
                    "budgets:ViewBudget",
                ],
                [f"arn:aws:budgets::{acct}:budget/software-factory*"],
            ),
            # --- CloudWatch alarms. Metric and dashboard calls accept no ARN.
            (
                [
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DeleteAlarms",
                    "cloudwatch:DescribeAlarms",
                    "cloudwatch:TagResource",
                    "cloudwatch:UntagResource",
                ],
                [f"arn:aws:cloudwatch:{region}:{acct}:alarm:SoftwareFactory*"],
            ),
            # --- Read the CDK asset bundles CloudFormation has to fetch.
            (
                ["s3:GetObject", "s3:GetBucketLocation", "s3:ListBucket"],
                [
                    f"arn:aws:s3:::{self.assets_bucket}",
                    f"arn:aws:s3:::{self.assets_bucket}/*",
                ],
            ),
            # --- KMS. CreateKey accepts no resource ARN (the key does not exist yet) and a
            # CDK-generated key has no predictable name, so this pair cannot be name-scoped.
            # shortcut: the one unscoped grant left. Upgrade path: give the key an explicit
            # alias in factory_stacks.py and condition these on kms:RequestAlias.
            (
                [
                    "kms:CreateKey",
                    "kms:CreateAlias",
                    "kms:DeleteAlias",
                    "kms:DescribeKey",
                    "kms:EnableKeyRotation",
                    "kms:DisableKeyRotation",
                    "kms:GetKeyRotationStatus",
                    "kms:GetKeyPolicy",
                    "kms:PutKeyPolicy",
                    "kms:ScheduleKeyDeletion",
                    "kms:TagResource",
                    "kms:UntagResource",
                    "kms:ListResourceTags",
                ],
                ["*"],
            ),
            # --- API Gateway v2. Its control-plane ARNs are paths, not names, and a create
            # call cannot be conditioned on a tag, so /apis is the tightest expressible scope.
            # shortcut: the pipeline can reach other HTTP APIs in this account. Upgrade path:
            # move the webhook API into its own account, or an SCP on apigateway:DELETE.
            (
                [
                    "apigateway:POST",
                    "apigateway:GET",
                    "apigateway:PATCH",
                    "apigateway:PUT",
                    "apigateway:DELETE",
                ],
                [
                    f"arn:aws:apigateway:{region}::/apis",
                    f"arn:aws:apigateway:{region}::/apis/*",
                    f"arn:aws:apigateway:{region}::/tags/*",
                ],
            ),
        ]
