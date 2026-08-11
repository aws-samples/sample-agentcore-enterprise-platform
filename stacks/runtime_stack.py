"""Runtime Stack — AgentCore Runtime with ECR + CodeBuild pipeline + source hash tracking.

Ports the proven pattern from sample-strands-agent-with-agentcore:
  ECR repo → S3 source upload → CodeBuild (ARM64) → Build waiter Lambda → CfnRuntime

Source hash tracking ensures:
  - Container rebuilds only when source code changes
  - Unchanged source produces no changes on `cdk diff`
  - ECR images tagged with both `latest` and source hash
"""

import os

import aws_cdk as cdk
from aws_cdk import (
    CustomResource,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_codebuild as codebuild,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_s3_assets as s3_assets,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from constructs import Construct

from infra_utils.runtime_network import build_network_config
from infra_utils.runtime_protocol import needs_jwt_authorizer, resolve_protocol
from infra_utils.source_hash import component_image_tag


class RuntimeStack(cdk.Stack):
    """Deploys a single AgentCore Runtime component (orchestrator, a2a_agent, or mcp_server).

    Instantiate multiple times for multi-agent architectures:
        RuntimeStack(app, "runtime-orchestrator", component_name="orchestrator", runtime_type="orchestrator", ...)
        RuntimeStack(app, "runtime-code-agent", component_name="code-agent", runtime_type="a2a_agent", ...)
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        component_name: str,
        source_dir: str,
        runtime_type: str = "orchestrator",
        cognito_issuer_url: str = "",
        cognito_allowed_clients: list[str] | None = None,
        network_mode: str = "PUBLIC",
        subnet_ids: list[str] | None = None,
        security_group_ids: list[str] | None = None,
        extra_env_vars: dict[str, str] | None = None,
        dockerfile_pattern: str = "",
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        # AgentCore names must use underscores, not hyphens
        rt_name = f"{project_name}_{environment}_{component_name}".replace("-", "_")

        # Resolve absolute source path
        repo_root = os.path.dirname(os.path.dirname(__file__))
        abs_source_dir = os.path.join(repo_root, source_dir)

        # ── Source Hash ──
        source_hash = component_image_tag(abs_source_dir, dockerfile_pattern)
        image_tag = source_hash

        # ── ECR Repository ──
        repo = ecr.Repository(
            self,
            "ECR",
            repository_name=f"{prefix}-{component_name}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            empty_on_delete=True,
            # Scan every pushed image: the agent images pull in a large Python
            # dependency tree, and this is the only automatic signal that a base
            # image or package has a known CVE (Checkov CKV_AWS_163).
            image_scan_on_push=True,
            # NOT setting encryption=KMS here, deliberately. ECR encryption is
            # immutable, so changing it requires replacing the repository, and
            # CloudFormation refuses to replace a resource with an explicit
            # repository_name ("cannot update a stack when a custom-named
            # resource requires replacing"). Verified: it rolls the stack back.
            # Retrofitting it therefore costs either the predictable repo name
            # that docs and scripts rely on, or a destroy/redeploy of every
            # runtime stack — a migration decision, tracked separately
            # (Checkov CKV_AWS_136). Repositories are encrypted with AES256 by
            # default in the meantime.
            lifecycle_rules=[
                ecr.LifecycleRule(max_image_count=10, description="Keep last 10 images")
            ],
        )

        # ── S3 Source Asset ──
        if os.path.isdir(abs_source_dir):
            source_asset = s3_assets.Asset(self, "SourceAsset", path=abs_source_dir)
        else:
            # Create a minimal placeholder so synth doesn't fail without agent code
            placeholder_dir = os.path.join(repo_root, ".cdk-placeholder")
            os.makedirs(placeholder_dir, exist_ok=True)
            placeholder_dockerfile = os.path.join(placeholder_dir, "Dockerfile")
            if not os.path.exists(placeholder_dockerfile):
                with open(placeholder_dockerfile, "w") as f:
                    f.write(
                        'FROM public.ecr.aws/docker/library/python:3.11-slim\nCMD ["echo","placeholder"]\n'
                    )
            source_asset = s3_assets.Asset(self, "SourceAsset", path=placeholder_dir)

        # ── CodeBuild Project (ARM64, privileged for Docker) ──
        docker_build_cmd = (
            "docker build --platform linux/arm64 -t $REPO_URI:$IMAGE_TAG ."
        )
        if dockerfile_pattern:
            docker_build_cmd = f"docker build --platform linux/arm64 -f {dockerfile_pattern}/Dockerfile -t $REPO_URI:$IMAGE_TAG ."

        build_project = codebuild.Project(
            self,
            "Build",
            project_name=f"{prefix}-build-{component_name}",
            description=f"Build container for {component_name}",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                privileged=True,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            timeout=cdk.Duration.minutes(30),
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "echo Logging in to Amazon ECR...",
                                (
                                    "aws ecr get-login-password --region $AWS_DEFAULT_REGION | "
                                    "docker login --username AWS --password-stdin "
                                    "$ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com"
                                ),
                                "echo Downloading source from S3...",
                                "aws s3 cp $SOURCE_S3_URI source.zip",
                                "mkdir -p source && unzip -o source.zip -d source/",
                            ],
                        },
                        "build": {
                            "commands": [
                                "cd source/",
                                docker_build_cmd,
                                "docker tag $REPO_URI:$IMAGE_TAG $REPO_URI:latest",
                            ],
                        },
                        "post_build": {
                            "commands": [
                                "echo Pushing image to ECR...",
                                "docker push $REPO_URI:$IMAGE_TAG",
                                "docker push $REPO_URI:latest",
                                "echo Build completed on `date`",
                            ],
                        },
                    },
                }
            ),
        )
        repo.grant_push(build_project)
        source_asset.grant_read(build_project)

        # ── Build Trigger Lambda (Custom Resource) ──
        trigger_fn = _lambda.Function(
            self,
            "BuildTrigger",
            function_name=f"{prefix}-build-trigger-{component_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="build_trigger_lambda.handler",
            code=_lambda.Code.from_asset(os.path.join(repo_root, "infra_utils")),
            timeout=cdk.Duration.minutes(15),
            memory_size=256,
        )
        trigger_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                resources=[build_project.project_arn],
            )
        )

        # Custom resource triggers build only when source hash changes
        build_trigger = CustomResource(
            self,
            "BuildTriggerCR",
            service_token=trigger_fn.function_arn,
            properties={
                "ProjectName": build_project.project_name,
                "EnvironmentOverrides": [
                    {
                        "name": "SOURCE_S3_URI",
                        "value": source_asset.s3_object_url,
                        "type": "PLAINTEXT",
                    },
                    {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
                    {
                        "name": "REPO_URI",
                        "value": repo.repository_uri,
                        "type": "PLAINTEXT",
                    },
                    {"name": "ACCOUNT_ID", "value": self.account, "type": "PLAINTEXT"},
                ],
                # Source hash change triggers rebuild
                "SourceHash": source_hash,
            },
        )

        # ── AgentCore Runtime IAM Role ──
        runtime_role = iam.Role(
            self,
            "RuntimeRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            role_name=f"{prefix}-{component_name}-runtime-role",
        )
        self._attach_runtime_permissions(
            runtime_role, repo.repository_arn, project_name=project_name
        )

        # ── Protocol Configuration ──
        protocol = resolve_protocol(runtime_type, dockerfile_pattern)

        # ── Environment Variables ──
        env_vars: dict[str, str] = {
            "PROJECT_NAME": project_name,
            "ENVIRONMENT": environment,
            "COMPONENT_NAME": component_name,
            "AWS_REGION_NAME": self.region,
            "SOURCE_HASH": source_hash,
        }
        env_vars.update(extra_env_vars or {})

        # ── Network Configuration ──
        # Shape and fail-fast rules live in infra_utils so they are testable
        # without CDK (tests/test_runtime_network.py).
        network_config = build_network_config(
            network_mode, subnet_ids, security_group_ids
        )

        # ── CfnRuntime ──
        runtime_props: dict = {
            "agent_runtime_name": rt_name,
            "role_arn": runtime_role.role_arn,
            "agent_runtime_artifact": {
                "containerConfiguration": {
                    "containerUri": f"{repo.repository_uri}:{image_tag}",
                },
            },
            "network_configuration": network_config,
            "protocol_configuration": protocol,
            "environment_variables": env_vars,
            # Forward Authorization to the container so agents can read JWT claims
            # (agent-code/shared/auth.py). Without this, context.request_headers is
            # empty and every JWT-consuming pattern fails at invoke time. Runtime
            # has already validated the token via the authorizer below, so the agent
            # decodes without re-verifying the signature. Docs: "Propagate a JWT
            # token to AgentCore Runtime" (runtime-oauth) + runtime-header-allowlist.
            "request_header_configuration": {
                "requestHeaderAllowlist": ["Authorization"],
            },
        }

        # CUSTOM_JWT auth for client-facing protocols (HTTP, MCP, AGUI).
        if needs_jwt_authorizer(protocol) and cognito_issuer_url:
            runtime_props["authorizer_configuration"] = {
                "customJwtAuthorizer": {
                    "discoveryUrl": f"{cognito_issuer_url}/.well-known/openid-configuration",
                    "allowedClients": cognito_allowed_clients or [],
                },
            }

        self._runtime = agentcore.CfnRuntime(self, "Runtime", **runtime_props)
        self._runtime.node.add_dependency(build_trigger)

        # ── SSM Parameters ──
        ssm.StringParameter(
            self,
            "SSMRuntimeArn",
            parameter_name=f"/{project_name}/{environment}/runtimes/{component_name}/arn",
            string_value=self._runtime.attr_agent_runtime_arn,
        )
        ssm.StringParameter(
            self,
            "SSMRuntimeId",
            parameter_name=f"/{project_name}/{environment}/runtimes/{component_name}/id",
            string_value=self._runtime.attr_agent_runtime_id,
        )

        # ── Outputs ──
        cdk.CfnOutput(
            self,
            "RuntimeArn",
            value=self._runtime.attr_agent_runtime_arn,
            export_name=f"{prefix}-{component_name}-runtime-arn",
        )
        cdk.CfnOutput(self, "RuntimeId", value=self._runtime.attr_agent_runtime_id)
        cdk.CfnOutput(self, "SourceHash", value=source_hash)
        cdk.CfnOutput(self, "ImageUri", value=f"{repo.repository_uri}:{image_tag}")

    @property
    def runtime_arn(self) -> str:
        return self._runtime.attr_agent_runtime_arn

    @property
    def runtime_id(self) -> str:
        return self._runtime.attr_agent_runtime_id

    @staticmethod
    def _attach_runtime_permissions(
        role: iam.Role, ecr_repo_arn: str, *, project_name: str
    ) -> None:
        """Attach the standard AgentCore Runtime permissions to a role.

        Wildcard resources are used only where the AWS action does not support
        resource-level permissions; each one says so. Everything that can be
        scoped is scoped, because this role is what a compromised agent inherits.
        """
        statements = [
            # ECR image pull, limited to this component's own repository. It used
            # to be resources=["*"], which let any runtime role pull every image
            # in the account (Checkov CKV_AWS_108) — the repo ARN was already
            # being passed to this function and simply ignored.
            iam.PolicyStatement(
                sid="ECRPull",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[ecr_repo_arn],
            ),
            # GetAuthorizationToken has no resource to scope to: it returns an
            # account-level token, so IAM only accepts "*".
            iam.PolicyStatement(
                sid="ECRAuth",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
            # CloudWatch Logs. PutResourcePolicy is what lets AgentCore allow
            # X-Ray to deliver this agent's spans into the agent's own log group
            # (the unified span destination) instead of the shared aws/spans
            # group. Kept scoped to the runtime log groups: account-wide it would
            # let the agent open any log group to another account.
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:PutResourcePolicy",
                ],
                resources=[
                    "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/runtimes/*"
                ],
            ),
            # X-Ray tracing. These two actions accept no resource ARN, so "*" is
            # the only valid value — not a missed scoping opportunity.
            iam.PolicyStatement(
                sid="XRayTracing",
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            ),
            # CloudWatch metrics. PutMetricData also takes no resource ARN; the
            # namespace condition is what constrains it instead.
            iam.PolicyStatement(
                sid="CloudWatchMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}
                },
            ),
            # Bedrock model invocation
            iam.PolicyStatement(
                sid="BedrockModels",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:*:inference-profile/*",
                ],
            ),
            # SSM Parameter Store (cross-stack discovery), scoped to this
            # project's own parameter path. On "*" the agent could read every
            # parameter in the account, which is the classic data-exfiltration
            # path Checkov CKV_AWS_108 looks for. infra_utils/agentcore_role.py
            # — the file that calls itself the reference policy — already scoped
            # these actions this way; this makes the two agree.
            iam.PolicyStatement(
                sid="SSMAccess",
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[f"arn:aws:ssm:*:*:parameter/{project_name}/*"],
            ),
            # AgentCore service access (Gateway, Memory, A2A, Registry, etc.).
            # Still "*", and knowingly so: scoping these needs the gateway,
            # memory and sibling-runtime ARNs, and the A2A targets do not exist
            # yet when this role is built. Tracked as its own task rather than
            # rushed here, because getting it wrong breaks every live-verified
            # path (gateway tools, memory, A2A delegation).
            iam.PolicyStatement(
                sid="AgentCoreAccess",
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeGateway",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:ListGateways",
                    # Memory data plane. ListEvents/ListSessions/DeleteEvent are
                    # required by the framework memory integrations (e.g. LangGraph's
                    # AgentCoreMemorySaver checkpointer lists events to rehydrate a
                    # thread); without them memory-backed patterns fail at invoke.
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:CreateWorkloadIdentity",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                resources=["*"],
            ),
            # AgentCore Identity token vault. GetResourceOauth2Token reads the
            # credential provider's client secret from a Secrets Manager secret
            # that AgentCore Identity owns, using the CALLER's identity — so
            # without this the gateway MCP token fetch fails with
            # AccessDeniedException and Strands aborts loading gateway tools.
            # Scoped to the vault's own secret path, not all secrets.
            iam.PolicyStatement(
                sid="AgentCoreIdentityTokenVaultSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    cdk.Stack.of(role).format_arn(
                        service="secretsmanager",
                        resource="secret",
                        resource_name="bedrock-agentcore-identity!default/oauth2/*",
                        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                    ),
                ],
            ),
        ]
        for stmt in statements:
            role.add_to_policy(stmt)
