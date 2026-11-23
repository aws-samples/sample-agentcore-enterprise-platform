"""GitHub Actions trust for the software factory's own deploys.

Three principals, each with one job:

* ``software-factory-gha-diff``  - what a pull request gets. Read-only: it can read the two
  stacks' templates and nothing else. No write action anywhere in the account.
* ``software-factory-gha-deploy`` - what the ``deploy`` GitHub environment gets, and only that
  environment. It can start a CloudFormation change set on the two factory stacks, upload CDK
  assets, and hand ``software-factory-cdk-exec`` to CloudFormation. It holds no permission to
  create or change any resource directly.
* ``software-factory-cdk-exec``  - the role CloudFormation itself acts as while it applies a
  change set. Only ``cloudformation.amazonaws.com`` can assume it, so the only way to use its
  permissions is to change one of the two stacks the deploy role can reach.

This stack deliberately lives OUTSIDE the set the deploy role may touch: the pipeline cannot
deploy ``SoftwareFactoryCicd``, so it cannot widen its own grant. Deploy this stack from a
workstation.

Why not the CDK bootstrap roles: ``cdk-hnb659fds-cfn-exec-role-*`` carries AdministratorAccess,
so granting the pipeline ``sts:AssumeRole`` on the bootstrap deploy role would hand GitHub an
administrator path to the whole account. The stacks therefore use
``CliCredentialsStackSynthesizer`` (no role assumption at all) and the workflow passes
``--role-arn`` pointing at ``software-factory-cdk-exec``.
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_iam as iam,
)
from constructs import Construct

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
STS_AUDIENCE = "sts.amazonaws.com"

# The stacks the pipeline is allowed to change. SoftwareFactoryCicd is absent on purpose.
DEPLOYABLE_STACKS = ("SoftwareFactory", "SoftwareFactoryData")

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
        github_repo: str,
        subject_prefix: str | None = None,
        deploy_environment: str = "deploy",
        diff_branches: tuple[str, ...] = ("factory/main-v2",),
        bootstrap_qualifier: str = "hnb659fds",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.github_repo = github_repo
        # Every OIDC subject starts with this. Plain `repo:<owner>/<name>` is the default, but
        # a repository with immutable subject claims turned on sends
        # `repo:<owner>@<orgId>/<name>@<repoId>` instead, and a trust policy written against
        # the plain form is simply refused -- `Not authorized to perform
        # sts:AssumeRoleWithWebIdentity`, with no hint as to which condition missed. Read the
        # live value with
        #   gh api /repos/<owner>/<name>/actions/oidc/customization/sub
        # and pass its sub_claim_prefix. The immutable form is the stronger one: the numeric
        # ids cannot be reused, so renaming or recreating the repository does not inherit the
        # trust.
        self.subject_prefix = subject_prefix or f"repo:{github_repo}"
        self.assets_bucket = (
            f"cdk-{bootstrap_qualifier}-assets-{self.account}-{self.region}"
        )
        self.bootstrap_version_param = (
            f"arn:aws:ssm:{self.region}:{self.account}:parameter"
            f"/cdk-bootstrap/{bootstrap_qualifier}/version"
        )

        provider_arn = self._oidc_provider_arn()
        self.exec_role = self._exec_role()
        self.diff_role = self._diff_role(provider_arn, diff_branches)
        self.deploy_role = self._deploy_role(provider_arn, deploy_environment)

        cdk.CfnOutput(self, "DiffRoleArn", value=self.diff_role.role_arn)
        cdk.CfnOutput(self, "DeployRoleArn", value=self.deploy_role.role_arn)
        cdk.CfnOutput(self, "CfnExecRoleArn", value=self.exec_role.role_arn)

    # ---------------------------------------------------------------- trust

    def _oidc_provider_arn(self) -> str:
        """One provider per account. Pass -c github_oidc_provider_arn=... to reuse an existing one.

        CfnOIDCProvider, not the L2 OpenIdConnectProvider: the L2 is backed by a custom
        resource whose Lambda holds iam:*OpenIDConnectProvider* on '*' for the life of the
        account. For a resource CloudFormation supports natively that is a standing privilege
        bought for nothing.
        """
        existing = self.node.try_get_context("github_oidc_provider_arn")
        if existing:
            return existing
        provider = iam.CfnOIDCProvider(
            self,
            "GitHubOidc",
            url=GITHUB_OIDC_URL,
            client_id_list=[STS_AUDIENCE],
            # IAM validates token.actions.githubusercontent.com against its own trusted-CA
            # store and ignores these, so a rotated GitHub certificate cannot break the
            # trust. The list is here because older CloudFormation regions still want one.
            thumbprint_list=[
                "6938fd4d98bab03faadb97b34396831e3780aea1",
                "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
            ],
        )
        return provider.attr_arn

    def _web_identity(
        self, provider_arn: str, subjects: list[str]
    ) -> iam.WebIdentityPrincipal:
        """A token is accepted only when BOTH the audience and the exact subject match.

        ``sub`` carries the repository, so a fork or a second repository produces a different
        subject and is refused. StringEquals, not StringLike: no wildcard that a branch named
        ``factory/main-v2-evil`` could satisfy.
        """
        return iam.WebIdentityPrincipal(
            provider_arn,
            {
                "StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": STS_AUDIENCE,
                    f"{GITHUB_OIDC_HOST}:sub": subjects,
                }
            },
        )

    # ---------------------------------------------------------------- roles

    def _diff_role(self, provider_arn: str, diff_branches: tuple[str, ...]) -> iam.Role:
        subjects = [f"{self.subject_prefix}:pull_request"] + [
            f"{self.subject_prefix}:ref:refs/heads/{b}" for b in diff_branches
        ]
        role = iam.Role(
            self,
            "GhaDiff",
            role_name="software-factory-gha-diff",
            assumed_by=self._web_identity(provider_arn, subjects),
            max_session_duration=cdk.Duration.hours(1),
            description="GitHub Actions: cdk diff on a pull request. Read-only.",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStackResources",
                    "cloudformation:GetTemplate",
                    "cloudformation:ListStackResources",
                    "cloudformation:GetStackPolicy",
                ],
                resources=self._stack_arns(),
            )
        )
        # GetTemplateSummary is called with the template body the CLI just synthesised, so it
        # names no stack and accepts no ARN. It reads nothing from the account.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:GetTemplateSummary"], resources=["*"]
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[self.bootstrap_version_param],
            )
        )
        return role

    def _deploy_role(self, provider_arn: str, deploy_environment: str) -> iam.Role:
        role = iam.Role(
            self,
            "GhaDeploy",
            role_name="software-factory-gha-deploy",
            assumed_by=self._web_identity(
                provider_arn,
                [f"{self.subject_prefix}:environment:{deploy_environment}"],
            ),
            max_session_duration=cdk.Duration.hours(1),
            description=(
                f"GitHub Actions: cdk deploy of {'/'.join(DEPLOYABLE_STACKS)} after approval "
                f"on the '{deploy_environment}' environment"
            ),
        )
        # Change the two factory stacks. No DeleteStack: this pipeline updates, it never
        # removes a stack, and SoftwareFactoryData holds the run history.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudformation:CreateStack",
                    "cloudformation:UpdateStack",
                    "cloudformation:CreateChangeSet",
                    "cloudformation:ExecuteChangeSet",
                    "cloudformation:DeleteChangeSet",
                    "cloudformation:DescribeChangeSet",
                    "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStackResources",
                    "cloudformation:GetTemplate",
                    "cloudformation:ListStackResources",
                    "cloudformation:GetStackPolicy",
                    "cloudformation:RollbackStack",
                    "cloudformation:ContinueUpdateRollback",
                    "cloudformation:TagResource",
                    "cloudformation:UntagResource",
                ],
                resources=self._stack_arns(),
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:GetTemplateSummary"], resources=["*"]
            )
        )
        # The only role it may hand to CloudFormation, and only to CloudFormation.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[self.exec_role.role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "cloudformation.amazonaws.com"
                    }
                },
            )
        )
        # CDK file assets (lambda bundles, the nested template) into the bootstrap bucket.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:AbortMultipartUpload",
                    "s3:ListBucketMultipartUploads",
                    "s3:ListMultipartUploadParts",
                ],
                resources=[
                    f"arn:aws:s3:::{self.assets_bucket}",
                    f"arn:aws:s3:::{self.assets_bucket}/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[self.bootstrap_version_param],
            )
        )
        return role

    def _stack_arns(self) -> list[str]:
        return [
            f"arn:aws:cloudformation:{self.region}:{self.account}:stack/{name}/*"
            for name in DEPLOYABLE_STACKS
        ]

    # ------------------------------------------------- CloudFormation's own role

    def _exec_role(self) -> iam.Role:
        """What CloudFormation may do while applying a change set to the factory stacks.

        Broad per service, but every resource is name-scoped to the factory, and the only
        caller is CloudFormation acting on a stack the deploy role is allowed to name.
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
