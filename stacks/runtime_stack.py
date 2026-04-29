"""Runtime Stack — AgentCore Runtime with ECR + CodeBuild pipeline + source hash tracking.

Ports the proven pattern from sample-strands-agent-with-agentcore:
  ECR repo → S3 source upload → CodeBuild (ARM64) → Build waiter Lambda → CfnRuntime

Source hash tracking ensures:
  - Container rebuilds only when source code changes
  - Unchanged source produces no changes on `cdk diff`
  - ECR images tagged with both `latest` and source hash
"""
import hashlib
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_codebuild as codebuild,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3_assets as s3_assets,
    aws_ssm as ssm,
    CustomResource,
)
from constructs import Construct

# Directories/extensions excluded from source hash computation
HASH_EXCLUDES = {
    "node_modules", "__pycache__", ".git", ".venv", ".next",
    ".terraform", ".DS_Store", ".pyc", ".log", ".egg-info",
    "dist", "build", "cdk.out",
}


def _compute_source_hash(source_path: str) -> str:
    """Compute a stable SHA-256 hash over all source files, excluding non-source artifacts."""
    if not os.path.isdir(source_path):
        return "placeholder"
    file_hashes = []
    for root, dirs, files in os.walk(source_path):
        # Prune excluded directories in-place
        dirs[:] = [d for d in sorted(dirs) if d not in HASH_EXCLUDES]
        for fname in sorted(files):
            if any(fname.endswith(ext) for ext in (".pyc", ".log", ".DS_Store")):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, source_path)
            h = hashlib.sha256()
            h.update(rel.encode())
            with open(fpath, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            file_hashes.append(h.hexdigest())
    if not file_hashes:
        return "empty"
    combined = hashlib.sha256("".join(file_hashes).encode())
    return combined.hexdigest()[:16]


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
        source_hash = _compute_source_hash(abs_source_dir)
        image_tag = source_hash

        # ── ECR Repository ──
        repo = ecr.Repository(self, "ECR",
            repository_name=f"{prefix}-{component_name}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10, description="Keep last 10 images")],
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
                    f.write("FROM public.ecr.aws/docker/library/python:3.11-slim\nCMD [\"echo\",\"placeholder\"]\n")
            source_asset = s3_assets.Asset(self, "SourceAsset", path=placeholder_dir)

        # ── CodeBuild Project (ARM64, privileged for Docker) ──
        build_project = codebuild.Project(self, "Build",
            project_name=f"{prefix}-build-{component_name}",
            description=f"Build container for {component_name}",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                privileged=True,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            timeout=cdk.Duration.minutes(30),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "pre_build": {
                        "commands": [
                            "echo Logging in to Amazon ECR...",
                            f"aws ecr get-login-password --region $AWS_DEFAULT_REGION | "
                            f"docker login --username AWS --password-stdin "
                            f"$ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
                            "echo Downloading source from S3...",
                            "aws s3 cp $SOURCE_S3_URI source.zip",
                            "mkdir -p source && unzip -o source.zip -d source/",
                        ],
                    },
                    "build": {
                        "commands": [
                            "cd source/",
                            "docker build --platform linux/arm64 -t $REPO_URI:$IMAGE_TAG .",
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
            }),
        )
        repo.grant_push(build_project)
        source_asset.grant_read(build_project)

        # ── Build Trigger Lambda (Custom Resource) ──
        trigger_fn = _lambda.Function(self, "BuildTrigger",
            function_name=f"{prefix}-build-trigger-{component_name}",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="build_trigger_lambda.handler",
            code=_lambda.Code.from_asset(os.path.join(repo_root, "infra_utils")),
            timeout=cdk.Duration.minutes(15),
            memory_size=256,
        )
        trigger_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
            resources=[build_project.project_arn],
        ))

        # Custom resource triggers build only when source hash changes
        build_trigger = CustomResource(self, "BuildTriggerCR",
            service_token=trigger_fn.function_arn,
            properties={
                "ProjectName": build_project.project_name,
                "EnvironmentOverrides": [
                    {"name": "SOURCE_S3_URI", "value": source_asset.s3_object_url, "type": "PLAINTEXT"},
                    {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
                    {"name": "REPO_URI", "value": repo.repository_uri, "type": "PLAINTEXT"},
                    {"name": "ACCOUNT_ID", "value": self.account, "type": "PLAINTEXT"},
                ],
                # Source hash change triggers rebuild
                "SourceHash": source_hash,
            },
        )

        # ── AgentCore Runtime IAM Role ──
        runtime_role = iam.Role(self, "RuntimeRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            role_name=f"{prefix}-{component_name}-runtime-role",
        )
        self._attach_runtime_permissions(runtime_role, repo.repository_arn)

        # ── Protocol Configuration ──
        protocol_map = {"orchestrator": "HTTP", "a2a_agent": "A2A", "mcp_server": "MCP"}
        protocol = protocol_map.get(runtime_type, "HTTP")

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
        network_config: dict = {"networkMode": network_mode}
        if network_mode == "VPC" and subnet_ids and security_group_ids:
            network_config["vpcConfiguration"] = {
                "subnetIds": subnet_ids,
                "securityGroupIds": security_group_ids,
            }

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
        }

        # CUSTOM_JWT auth for HTTP and MCP runtimes
        if protocol in ("HTTP", "MCP") and cognito_issuer_url:
            runtime_props["authorizer_configuration"] = {
                "customJwtAuthorizer": {
                    "discoveryUrl": f"{cognito_issuer_url}/.well-known/openid-configuration",
                    "allowedAudience": cognito_allowed_clients or [],
                },
            }

        self._runtime = agentcore.CfnRuntime(self, "Runtime", **runtime_props)
        self._runtime.node.add_dependency(build_trigger)

        # ── SSM Parameters ──
        ssm.StringParameter(self, "SSMRuntimeArn",
            parameter_name=f"/{project_name}/{environment}/runtimes/{component_name}/arn",
            string_value=self._runtime.attr_agent_runtime_arn,
        )
        ssm.StringParameter(self, "SSMRuntimeId",
            parameter_name=f"/{project_name}/{environment}/runtimes/{component_name}/id",
            string_value=self._runtime.attr_agent_runtime_id,
        )

        # ── Outputs ──
        cdk.CfnOutput(self, "RuntimeArn", value=self._runtime.attr_agent_runtime_arn,
                       export_name=f"{prefix}-{component_name}-runtime-arn")
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
    def _attach_runtime_permissions(role: iam.Role, ecr_repo_arn: str) -> None:
        """Attach the standard AgentCore Runtime permissions to a role."""
        statements = [
            # ECR image pull
            iam.PolicyStatement(
                sid="ECRAccess",
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
            # CloudWatch Logs
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=["arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/runtimes/*"],
            ),
            # X-Ray tracing
            iam.PolicyStatement(
                sid="XRayTracing",
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            ),
            # CloudWatch metrics
            iam.PolicyStatement(
                sid="CloudWatchMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            ),
            # Bedrock model invocation
            iam.PolicyStatement(
                sid="BedrockModels",
                actions=["bedrock:InvokeModel", "bedrock:Converse", "bedrock:ConverseStream"],
                resources=["arn:aws:bedrock:*::foundation-model/*"],
            ),
            # SSM Parameter Store (cross-stack discovery)
            iam.PolicyStatement(
                sid="SSMAccess",
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=["*"],
            ),
            # AgentCore service access (Gateway, Memory, A2A, Registry, etc.)
            iam.PolicyStatement(
                sid="AgentCoreAccess",
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeGateway",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:ListGateways",
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:CreateWorkloadIdentity",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                ],
                resources=["*"],
            ),
        ]
        for stmt in statements:
            role.add_to_policy(stmt)
