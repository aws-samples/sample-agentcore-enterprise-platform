"""claude-3p-on-bedrock — Claude Code and Claude Desktop/Cowork on Amazon Bedrock
through Anthropic's Claude apps gateway, on the platform's private network.

Developers sign in with Microsoft Entra ID: no AWS credentials or API keys on
laptops. The gateway runs on ECS Fargate (Graviton) behind an INTERNAL ALB in
the platform VPC, keeps sign-in state in RDS PostgreSQL, and calls Bedrock
with its task role, scoped to exactly the models platform.yaml lists.

  developer (VPN / corporate network, client_cidrs only)
     │ https://<hostname>   Route 53 private zone → internal ALB (TLS 1.3)
     ▼
  ECS Fargate task
     ├─ init container: writes /config/gateway.yaml from the task definition
     └─ claude gateway   ──► RDS PostgreSQL (verify-full TLS)
                         ──► Entra ID (OIDC, confidential client)
                         ──► Amazon Bedrock (task role; optional guardrail)

Two change paths, deliberately separate:
  * models / teams / policies → platform.yaml → `deploy.sh build`: a new task
    definition revision and a rolling deploy. No image build.
  * claude_version → the image is rebuilt (CodeBuild ARM64, binary verified
    against Anthropic's GPG-signed manifest) and rolled out.

Platform interface consumed: {ssm}/networking/vpc-id, {ssm}/networking/private-subnet-ids
Published: {ssm}/use-cases/claude-3p-on-bedrock/{public-url,oauth-redirect-uri,
           bootstrap-url,probe-function}

Grounded in https://code.claude.com/docs/en/claude-apps-gateway-on-aws and
https://code.claude.com/docs/en/claude-apps-gateway-config.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as r53_targets
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
USE_CASE = "claude-3p-on-bedrock"

# Release verification (code.claude.com/docs/en/setup#binary-integrity-and-code-signing).
RELEASE_KEY_URL = "https://downloads.claude.ai/keys/claude-code.asc"
RELEASE_KEY_FPR = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"  # gitleaks:allow (public GPG key fingerprint)
RELEASES_URL = "https://downloads.claude.ai/claude-code-releases"
RDS_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
BINARY_PLATFORM = "linux-arm64"
# Writes the config file and exits. Multi-arch index pinned by digest; for
# production, mirror it into a private ECR repository (walkthrough.md).
INIT_IMAGE = (
    "public.ecr.aws/docker/library/busybox:1.38.0"
    "@sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4"
)


def _load_config_module():
    """Import gateway/config.py under a unique module name (app.py
    loads this file by path, so a bare import would depend on sys.path)."""
    name = "use_case_claude_3p_on_bedrock_config"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, HERE / "gateway" / "config.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


gc = _load_config_module()


def _image_tag(claude_version: str) -> str:
    """Version + Dockerfile hash: the image changes only when either does."""
    digest = hashlib.sha256(
        (HERE / "gateway" / "image" / "Dockerfile").read_bytes()
    ).hexdigest()[:12]
    return f"{claude_version}-{digest}"


class Claude3pOnBedrockStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, ctx: dict, cfg, **kw):
        super().__init__(scope, id, **kw)
        name = f"{ctx['prefix']}-c3p"
        removal = cdk.RemovalPolicy.RETAIN if cfg.retain else cdk.RemovalPolicy.DESTROY
        bedrock_region = cfg.region_for_bedrock(self.region)

        if not cdk.Token.is_unresolved(self.region):
            cert_region = cfg.certificate_arn.split(":")[3]
            if cert_region != self.region:
                raise ValueError(
                    f"use_cases.{USE_CASE}.certificate_arn is in {cert_region} but the stack "
                    f"deploys to {self.region}; ACM certificates are regional"
                )

        # ── Platform VPC from the networking stack's SSM parameters ───────────
        # Deploy-time resolution, so synth works without the platform deployed.
        # The networking stack creates exactly two private subnets (max_azs=2).
        vpc_id = ssm.StringParameter.value_for_string_parameter(
            self, f"{ctx['ssm_prefix']}/networking/vpc-id"
        )
        subnet_csv = ssm.StringParameter.value_for_string_parameter(
            self, f"{ctx['ssm_prefix']}/networking/private-subnet-ids"
        )
        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "PlatformVpc",
            vpc_id=vpc_id,
            availability_zones=[cdk.Fn.select(i, cdk.Fn.get_azs()) for i in range(2)],
            private_subnet_ids=[
                cdk.Fn.select(i, cdk.Fn.split(",", subnet_csv, 2)) for i in range(2)
            ],
        )
        private = ec2.SubnetSelection(subnets=vpc.private_subnets)

        # ── Security groups: client_cidrs → ALB:443 → task:8080 → RDS:5432 ────
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=vpc,
            allow_all_outbound=False,
            description="Claude apps gateway ALB: 443 from developer networks only",
        )
        for i, cidr in enumerate(cfg.client_cidrs):
            alb_sg.add_ingress_rule(
                ec2.Peer.ipv4(cidr), ec2.Port.tcp(443), f"developers {i}"
            )
        task_sg = ec2.SecurityGroup(
            self,
            "TaskSg",
            vpc=vpc,
            allow_all_outbound=False,
            description="Claude apps gateway tasks: 8080 from the ALB only",
        )
        task_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8080), "ALB to gateway")
        alb_sg.add_egress_rule(task_sg, ec2.Port.tcp(8080), "ALB to gateway")
        # Entra, Bedrock, ECR, Secrets Manager, CloudWatch: all HTTPS.
        task_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS egress")
        db_sg = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=vpc,
            allow_all_outbound=False,
            description="Claude apps gateway Postgres: 5432 from gateway tasks only",
        )
        db_sg.add_ingress_rule(task_sg, ec2.Port.tcp(5432), "gateway tasks to Postgres")
        task_sg.add_egress_rule(db_sg, ec2.Port.tcp(5432), "gateway tasks to Postgres")
        probe_sg = ec2.SecurityGroup(
            self,
            "ProbeSg",
            vpc=vpc,
            allow_all_outbound=False,
            description="In-VPC health probe used by verify.py",
        )
        probe_sg.add_egress_rule(alb_sg, ec2.Port.tcp(443), "probe to ALB")
        alb_sg.add_ingress_rule(probe_sg, ec2.Port.tcp(443), "in-VPC probe")

        # ── RDS PostgreSQL ─────────────────────────────────────────────────────
        engine = rds.DatabaseInstanceEngine.postgres(
            version=rds.PostgresEngineVersion.VER_16
        )
        db = rds.DatabaseInstance(
            self,
            "Db",
            engine=engine,
            instance_type=ec2.InstanceType(cfg.database.instance_class),
            vpc=vpc,
            vpc_subnets=private,
            security_groups=[db_sg],
            database_name=gc.DB_NAME,
            # Secrets Manager secret generated by CDK and attached to the
            # instance. The gateway runs its own schema migrations at boot, so
            # it connects as the owner role.
            credentials=rds.Credentials.from_generated_secret("gateway"),
            allocated_storage=cfg.database.allocated_storage_gib,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            multi_az=cfg.database.multi_az,
            publicly_accessible=False,
            backup_retention=cdk.Duration.days(cfg.database.backup_retention_days),
            deletion_protection=cfg.retain,
            removal_policy=cdk.RemovalPolicy.SNAPSHOT
            if cfg.retain
            else cdk.RemovalPolicy.DESTROY,
            parameter_group=rds.ParameterGroup(
                self, "DbParams", engine=engine, parameters={"rds.force_ssl": "1"}
            ),
        )

        # ── Secrets: session key generated here; Entra secret by NAME only ────
        jwt_secret = secretsmanager.Secret(
            self,
            "JwtSecret",
            # No fixed secret_name: a deleted secret keeps its name reserved
            # for its recovery window, so a stack rollback or destroy followed
            # by a redeploy would fail with "already scheduled for deletion".
            # CloudFormation generates a unique name instead.
            description="Claude apps gateway session JWT signing key",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=48, exclude_punctuation=True
            ),
            removal_policy=removal,
        )
        oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OidcSecret", cfg.oidc.client_secret_name
        )

        # ── Image: config-free, rebuilt only when claude_version changes ──────
        image_tag = _image_tag(cfg.claude_version)
        repo = ecr.Repository(
            self,
            "Repo",
            repository_name=f"{name}-gateway",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            removal_policy=removal,
            empty_on_delete=not cfg.retain,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
        )
        source = s3_assets.Asset(
            self, "ImageContext", path=str(HERE / "gateway" / "image")
        )
        build = codebuild.Project(
            self,
            "Build",
            project_name=f"{name}-gateway-build",
            description="Build the Claude apps gateway image from a verified Claude Code release",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                privileged=True,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            timeout=cdk.Duration.minutes(30),
            build_spec=codebuild.BuildSpec.from_object(_buildspec()),
        )
        repo.grant_pull_push(build)
        # The "image already exists" check; without it the denial reads as
        # "not found" and a rebuild pushes onto an immutable tag and fails.
        build.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecr:DescribeImages"], resources=[repo.repository_arn]
            )
        )
        source.grant_read(build)
        # Same build-waiter custom resource the runtime stacks use.
        trigger = _lambda.Function(
            self,
            "BuildTrigger",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="build_trigger_lambda.handler",
            code=_lambda.Code.from_asset(str(REPO_ROOT / "infra_utils")),
            timeout=cdk.Duration.minutes(15),
            memory_size=256,
        )
        trigger.add_to_role_policy(
            iam.PolicyStatement(
                actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                resources=[build.project_arn],
            )
        )
        image_build = cdk.CustomResource(
            self,
            "ImageBuild",
            service_token=trigger.function_arn,
            properties={
                "ProjectName": build.project_name,
                "SourceHash": image_tag,  # changes → CloudFormation update → new build
                "EnvironmentOverrides": [
                    {
                        "name": "SOURCE_S3_URI",
                        "value": source.s3_object_url,
                        "type": "PLAINTEXT",
                    },
                    {
                        "name": "REPO_URI",
                        "value": repo.repository_uri,
                        "type": "PLAINTEXT",
                    },
                    {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
                    {
                        "name": "CLAUDE_VERSION",
                        "value": cfg.claude_version,
                        "type": "PLAINTEXT",
                    },
                ],
            },
        )

        # ── Task role: Bedrock on the configured models only ──────────────────
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Claude apps gateway: Bedrock invoke on the configured models only",
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockConfiguredModels",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    # Free token counting for aborted requests; without it the
                    # gateway falls back to a one-token billable request.
                    "bedrock:CountTokens",
                ],
                resources=gc.bedrock_resource_arns(
                    cfg, bedrock_region, self.account, self.partition
                ),
            )
        )
        if cfg.guardrail:
            guardrail_arn = (
                cfg.guardrail.id
                if cfg.guardrail.id.startswith("arn:")
                else self.format_arn(
                    service="bedrock",
                    region=bedrock_region,
                    resource="guardrail",
                    resource_name=cfg.guardrail.id,
                )
            )
            task_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ApplyGuardrail",
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[guardrail_arn],
                )
            )

        # ── Task definition: init container writes the config, gateway reads it
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/{ctx['project']}/{ctx['environment']}/use-cases/{USE_CASE}",
            retention={
                30: logs.RetentionDays.ONE_MONTH,
                90: logs.RetentionDays.THREE_MONTHS,
                180: logs.RetentionDays.SIX_MONTHS,
                365: logs.RetentionDays.ONE_YEAR,
            }[cfg.log_retention_days],
            removal_policy=removal,
        )
        task_def = ecs.FargateTaskDefinition(
            self,
            "Task",
            cpu=cfg.service.cpu,
            memory_limit_mib=cfg.service.memory_mib,
            task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_def.add_volume(name="config")  # task-scoped, discarded with the task

        rendered = gc.render_gateway_yaml(cfg, self.region)
        # Base64 so the YAML's ${VAR} references and newlines pass through the
        # task definition untouched (no cfn-lint Fn::Sub ambiguity, no quoting).
        encoded = base64.b64encode(rendered.encode()).decode()
        if len(encoded) > gc.MAX_ENCODED_CONFIG_BYTES:
            # Task definitions are capped at 64 KiB in total.
            raise ValueError(
                f"use_cases.{USE_CASE}: rendered gateway.yaml is {len(encoded)} bytes encoded, over "
                f"{gc.MAX_ENCODED_CONFIG_BYTES}; trim teams/settings or move the config to S3"
            )
        config_writer = task_def.add_container(
            "config",
            image=ecs.ContainerImage.from_registry(INIT_IMAGE),
            essential=False,
            # The rendered gateway.yaml travels as task-definition data: any
            # change is a new revision, which ECS rolls out. No secrets in it.
            environment={"GATEWAY_YAML_B64": encoded},
            command=[
                "sh",
                "-c",
                f'printf "%s" "$GATEWAY_YAML_B64" | base64 -d > {gc.CONFIG_PATH}',
            ],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="config", log_group=log_group
            ),
            memory_reservation_mib=32,
        )
        config_writer.add_mount_points(
            ecs.MountPoint(
                container_path=gc.CONFIG_DIR, source_volume="config", read_only=False
            )
        )

        environment = {
            "CLAUDE_GATEWAY_LOG_LEVEL": "info",
            "DB_HOST": db.db_instance_endpoint_address,
        }
        if cfg.telemetry.mode == "sidecar":
            # Needed for the localhost collector (documented SSRF-guard tradeoff).
            environment["CLAUDE_GATEWAY_ALLOW_LOOPBACK"] = "1"
        if cfg.agentcore_tools:
            # Resolved by CloudFormation at deploy time, not at synth: the
            # platform's gateway stack deploys before use-case stacks, so a
            # first `deploy.sh build` works and a recreated gateway's new URL
            # reaches laptops on the next deploy. gateway.yaml holds
            # ${AGENTCORE_GATEWAY_URL}, which the gateway expands at boot.
            environment[gc.AGENTCORE_URL_VAR] = (
                ssm.StringParameter.value_for_string_parameter(
                    self, f"{ctx['ssm_prefix']}/gateway/url"
                )
            )
        gateway = task_def.add_container(
            "web",
            image=ecs.ContainerImage.from_ecr_repository(repo, image_tag),
            environment=environment,
            secrets={
                "GATEWAY_JWT_SECRET": ecs.Secret.from_secrets_manager(jwt_secret),
                "OIDC_CLIENT_SECRET": ecs.Secret.from_secrets_manager(oidc_secret),
                "DB_USER": ecs.Secret.from_secrets_manager(db.secret, "username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db.secret, "password"),
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="gateway", log_group=log_group
            ),
            port_mappings=[ecs.PortMapping(container_port=8080)],
            # On SIGTERM the gateway drains open streams for up to 25 s; ECS
            # must not SIGKILL before that.
            stop_timeout=cdk.Duration.seconds(40),
        )
        gateway.add_mount_points(
            ecs.MountPoint(
                container_path=gc.CONFIG_DIR, source_volume="config", read_only=True
            )
        )
        gateway.add_container_dependencies(
            ecs.ContainerDependency(
                container=config_writer,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )

        if cfg.telemetry.mode == "sidecar":
            task_role.add_to_policy(
                iam.PolicyStatement(
                    sid="OtlpMetricsToCloudWatch",
                    actions=["cloudwatch:PutMetricData"],
                    resources=["*"],  # PutMetricData supports no resource-level scoping
                )
            )
            task_def.add_container(
                "otel-collector",
                image=ecs.ContainerImage.from_registry(cfg.telemetry.collector_image),
                essential=False,  # telemetry must never take the gateway down
                environment={"AOT_CONFIG_CONTENT": _adot_config(self.region)},
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="otel", log_group=log_group
                ),
                memory_reservation_mib=128,
            )

        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_def,
            desired_count=cfg.service.desired_count,
            min_healthy_percent=100,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            vpc_subnets=private,
            security_groups=[task_sg],
            assign_public_ip=False,
            health_check_grace_period=cdk.Duration.seconds(120),
        )
        service.node.add_dependency(image_build)
        # Found live: without this, CloudFormation creates the service while the
        # execution role's policy (which waits on the RDS secret) is still being
        # created, so the first tasks fail with AccessDenied on GetSecretValue.
        exec_policy = task_def.obtain_execution_role().node.try_find_child(
            "DefaultPolicy"
        )
        if exec_policy is not None:
            service.node.add_dependency(exec_policy)

        # ── Internal ALB, IPv4 only (dual-stack internal ALBs break /login) ───
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=False,
            vpc_subnets=private,
            security_group=alb_sg,
            ip_address_type=elbv2.IpAddressType.IPV4,
            idle_timeout=cdk.Duration.seconds(3600),  # long streaming responses
            drop_invalid_header_fields=True,
        )
        listener = alb.add_listener(
            "Https",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[
                acm.Certificate.from_certificate_arn(self, "Cert", cfg.certificate_arn)
            ],
            ssl_policy=elbv2.SslPolicy.TLS13_RES,
            open=False,  # ingress only from the alb_sg rules above
        )
        listener.add_targets(
            "Gateway",
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[
                service.load_balancer_target(
                    container_name=gateway.container_name, container_port=8080
                )
            ],
            # /healthz, not /readyz: a Postgres blip must not drain every replica.
            health_check=elbv2.HealthCheck(path="/healthz", healthy_http_codes="200"),
            # Open streams keep flowing this long on a replaced task (see drain_seconds).
            deregistration_delay=cdk.Duration.seconds(cfg.service.drain_seconds),
        )

        # ── DNS: <hostname> → internal ALB ───────────────────────────────────
        # customer mode: nothing here; the customer CNAMEs hostname to the
        # AlbDnsName output in corporate DNS. route53 mode: alias record in a
        # private zone on the platform VPC.
        if cfg.dns.mode == "route53":
            pz = cfg.dns.private_zone
            if pz.id:
                zone = route53.PrivateHostedZone.from_hosted_zone_attributes(
                    self, "Zone", hosted_zone_id=pz.id, zone_name=pz.name
                )
            else:
                zone = route53.PrivateHostedZone(
                    self, "Zone", zone_name=pz.name, vpc=vpc
                )
            route53.ARecord(
                self,
                "Record",
                zone=zone,
                record_name=cfg.hostname.removesuffix(pz.name).rstrip(".") or None,
                target=route53.RecordTarget.from_alias(
                    r53_targets.LoadBalancerTarget(alb)
                ),
            )

        # ── In-VPC probe (what verify.py invokes) ────────────────────────────
        probe = _lambda.Function(
            self,
            "Probe",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(HERE / "gateway" / "probe")),
            timeout=cdk.Duration.seconds(15),
            memory_size=128,
            vpc=vpc,
            vpc_subnets=private,
            security_groups=[probe_sg],
            environment={
                "PUBLIC_URL": cfg.public_url,
                # The probe connects to the ALB by its own DNS name and sends
                # the gateway hostname as SNI and Host, so it works whether the
                # hostname lives in Route 53 or only in corporate DNS.
                "CONNECT_HOST": alb.load_balancer_dns_name,
                "INSECURE_TLS": "true" if cfg.certificate_private_ca else "false",
                # With the in-gateway IP allow-list on, the probe is refused
                # like any non-developer address: expect that refusal.
                "EXPECT_METADATA_STATUS": "403" if cfg.gateway_ip_allowlist else "200",
            },
            log_group=logs.LogGroup(
                self,
                "ProbeLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )

        # ── Publish under this use case's SSM namespace ──────────────────────
        ns = f"{ctx['ssm_prefix']}/use-cases/{USE_CASE}"
        for key, value in (
            ("public-url", cfg.public_url),
            ("oauth-redirect-uri", f"{cfg.public_url}/oauth/callback"),
            ("bootstrap-url", f"{cfg.public_url}/user/bootstrap"),
            ("probe-function", probe.function_name),
            # CNAME target for the customer's DNS record (dns.mode: customer).
            ("alb-dns-name", alb.load_balancer_dns_name),
            *(
                # verify.py checks the AgentCore Gateway points sign-in here.
                [("agentcore-tools-issuer", cfg.oidc.issuer)]
                if cfg.agentcore_tools
                else []
            ),
        ):
            ssm.StringParameter(
                self,
                "Param" + key.title().replace("-", ""),
                parameter_name=f"{ns}/{key}",
                string_value=value,
            )

        cdk.CfnOutput(self, "PublicUrl", value=cfg.public_url)
        cdk.CfnOutput(
            self,
            "AlbDnsName",
            value=alb.load_balancer_dns_name,
            description=(
                f"Corporate DNS: CNAME {cfg.hostname} to this name"
                if cfg.dns.mode == "customer"
                else "Alias target of the Route 53 record (created by this stack)"
            ),
        )
        cdk.CfnOutput(
            self,
            "OAuthRedirectUri",
            value=f"{cfg.public_url}/oauth/callback",
            description="Register on the Entra app registration (Web platform)",
        )
        cdk.CfnOutput(
            self, "DesktopBootstrapUrl", value=f"{cfg.public_url}/user/bootstrap"
        )
        cdk.CfnOutput(self, "ImageTag", value=image_tag)
        cdk.CfnOutput(self, "ProbeFunction", value=probe.function_name)


def _buildspec() -> dict:
    """Download the pinned release, verify signature and checksum, build, push.

    Per code.claude.com/docs/en/setup#binary-integrity-and-code-signing: the
    manifest's detached signature must be valid AND made by Anthropic's release
    key (fingerprint pinned), and the binary must match the manifest's sha256.
    """
    return {
        "version": "0.2",
        "env": {"shell": "bash"},
        "phases": {
            "pre_build": {
                # CodeBuild fails the phase on the first command that exits non-zero.
                "commands": [
                    (
                        "aws ecr get-login-password --region $AWS_DEFAULT_REGION"
                        " | docker login --username AWS --password-stdin ${REPO_URI%%/*}"
                    ),
                    # Immutable tags: an existing tag means this exact image exists.
                    (
                        "if aws ecr describe-images --repository-name ${REPO_URI#*/}"
                        " --image-ids imageTag=$IMAGE_TAG >/dev/null 2>&1; then touch /tmp/skip; fi"
                    ),
                    "aws s3 cp $SOURCE_S3_URI ctx.zip && mkdir ctx && unzip -q ctx.zip -d ctx && cd ctx",
                    "export GNUPGHOME=$(mktemp -d)",
                    f"curl -fsSL {RELEASE_KEY_URL} -o release-key.asc && gpg --batch --quiet --import release-key.asc",
                    f"curl -fsSL {RELEASES_URL}/$CLAUDE_VERSION/manifest.json -o manifest.json",
                    f"curl -fsSL {RELEASES_URL}/$CLAUDE_VERSION/manifest.json.sig -o manifest.json.sig",
                    "gpg --batch --status-fd 1 --verify manifest.json.sig manifest.json > gpg.status",
                    # VALIDSIG's last field is the primary key fingerprint.
                    (
                        f"grep -Eq '^\\[GNUPG:\\] VALIDSIG .* {RELEASE_KEY_FPR}$' gpg.status"
                        " || { echo 'manifest not signed by the Anthropic release key'; exit 1; }"
                    ),
                    (
                        "python3 -c \"import json,sys; m=json.load(open('manifest.json'));"
                        " sys.exit(0 if m['version']==sys.argv[1] else 'manifest version mismatch')\""
                        " $CLAUDE_VERSION"
                    ),
                    (
                        'EXPECTED=$(python3 -c "import json;'
                        f" print(json.load(open('manifest.json'))['platforms']['{BINARY_PLATFORM}']['checksum'])\")"
                    ),
                    f"curl -fsSL {RELEASES_URL}/$CLAUDE_VERSION/{BINARY_PLATFORM}/claude -o claude",
                    'echo "$EXPECTED  claude" | sha256sum -c -',
                    f"curl -fsSL {RDS_CA_BUNDLE_URL} -o rds-global-bundle.pem",
                    "grep -q 'BEGIN CERTIFICATE' rds-global-bundle.pem",
                ],
            },
            "build": {
                "commands": [
                    (
                        "[ -f /tmp/skip ] || docker build --platform linux/arm64 --provenance=false"
                        " -t $REPO_URI:$IMAGE_TAG ."
                    ),
                ],
            },
            "post_build": {
                "commands": [
                    '[ "$CODEBUILD_BUILD_SUCCEEDING" = 1 ] || exit 1',
                    "[ -f /tmp/skip ] || docker push $REPO_URI:$IMAGE_TAG",
                ],
            },
        },
    }


def _adot_config(region: str) -> str:
    """OTLP receiver on localhost → CloudWatch's native OTLP metrics endpoint (SigV4)."""
    return "\n".join(
        [
            "extensions:",
            "  sigv4auth:",
            "    service: monitoring",
            f"    region: {region}",
            "receivers:",
            "  otlp:",
            "    protocols:",
            "      http:",
            "        endpoint: 127.0.0.1:4318",
            "processors:",
            "  batch:",
            "    timeout: 10s",
            "exporters:",
            "  otlphttp:",
            f"    metrics_endpoint: https://monitoring.{region}.amazonaws.com/v1/metrics",
            "    auth:",
            "      authenticator: sigv4auth",
            "    compression: gzip",
            "service:",
            "  extensions: [sigv4auth]",
            "  pipelines:",
            "    metrics:",
            "      receivers: [otlp]",
            "      processors: [batch]",
            "      exporters: [otlphttp]",
        ]
    )


def build(app, ctx: dict, config: dict) -> None:
    """Entry point the platform calls when platform.yaml enables this use case."""
    try:
        cfg = gc.load(config)
    except ValueError as e:  # pydantic.ValidationError subclasses ValueError
        raise ValueError(
            f"use_cases.{USE_CASE} in platform.yaml is invalid:\n{e}"
        ) from None
    stack = Claude3pOnBedrockStack(
        app,
        f"{ctx['prefix']}-uc-{USE_CASE}",
        ctx=ctx,
        cfg=cfg,
        env=ctx["cdk_env"],
        description="Claude Code and Desktop/Cowork on Bedrock via the Claude apps gateway (Entra SSO)",
    )
    if cfg.agentcore_tools:
        # The task reads <ssm_prefix>/gateway/url when CloudFormation deploys it, so
        # the platform's gateway stack must deploy first. Declared, not left to
        # construct order: `deploy --stack "<networking> <use case>"` then pulls the
        # gateway in (and fails here, at synth, when it isn't in the footprint).
        gateway = app.node.try_find_child(f"{ctx['prefix']}-gateway")
        if gateway is None:
            raise ValueError(
                f"use_cases.{USE_CASE}.agentcore_tools needs the platform's gateway "
                f"stack ({ctx['prefix']}-gateway), which isn't part of this deployment. "
                "Add it to the platform, or remove agentcore_tools."
            )
        stack.add_dependency(gateway)
