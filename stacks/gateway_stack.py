"""Gateway Stack — AgentCore Gateway with Lambda tool targets."""
import aws_cdk as cdk
from aws_cdk import (
    aws_bedrock as bedrock,
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from constructs import Construct

from infra_utils.policy_loader import load_control


class GatewayStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, project_name: str, environment: str,
                 cognito_issuer_url: str, cognito_allowed_clients: list[str],
                 tool_configs: dict | None = None, enable_egress_filter: bool = False,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        gw_name = f"{project_name}-{environment}-gateway"

        # Gateway IAM role
        gw_role = iam.Role(self, "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            role_name=f"{prefix}-gateway-role",
        )
        gw_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:{prefix}-tool-*"],
        ))

        # ── Optional: egress Lambda interceptor + Bedrock Guardrail (control-library) ──
        # Applies a Bedrock Guardrail to Gateway REQUEST/RESPONSE for PII masking and
        # prompt-injection blocking. Built before the Gateway so it can reference the ARN.
        self._interceptor_fn = None
        interceptor_configurations = None
        if enable_egress_filter:
            guardrail_cfg = load_control("guardrail.egress-default")
            guardrail = bedrock.CfnGuardrail(self, "EgressGuardrail",
                name=f"{prefix}-egress-guardrail",
                description="Egress guardrail for AgentCore Gateway (PII + prompt injection).",
                blocked_input_messaging=guardrail_cfg["BlockedInputMessaging"],
                blocked_outputs_messaging=guardrail_cfg["BlockedOutputsMessaging"],
            )
            # Policy blocks come straight from the control-library artifact (CFN shape).
            for cfn_key in ("ContentPolicyConfig", "SensitiveInformationPolicyConfig",
                            "TopicPolicyConfig", "WordPolicyConfig",
                            "ContextualGroundingPolicyConfig"):
                if cfn_key in guardrail_cfg:
                    guardrail.add_property_override(cfn_key, guardrail_cfg[cfn_key])

            interceptor_role = iam.Role(self, "InterceptorRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
                ],
            )
            interceptor_role.add_to_policy(iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            ))

            self._interceptor_fn = _lambda.Function(self, "EgressInterceptor",
                function_name=f"{prefix}-egress-interceptor",
                runtime=_lambda.Runtime.PYTHON_3_13,
                handler="handler.handler",
                code=_lambda.Code.from_asset("tools/egress_interceptor"),
                architecture=_lambda.Architecture.ARM_64,
                timeout=cdk.Duration.seconds(30),
                role=interceptor_role,
                environment={
                    "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                    "GUARDRAIL_VERSION": guardrail.attr_version,
                },
            )

            interceptor_configurations = [
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST", "RESPONSE"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=self._interceptor_fn.function_arn,
                        ),
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=False,
                    ),
                ),
            ]

        # Gateway
        self._gateway = agentcore.CfnGateway(self, "Gateway",
            name=gw_name,
            role_arn=gw_role.role_arn,
            authorizer_type="CUSTOM_JWT",
            protocol_type="MCP",
            exception_level="DEBUG",
            authorizer_configuration={
                "customJwtAuthorizer": {
                    "discoveryUrl": f"{cognito_issuer_url}/.well-known/openid-configuration",
                    "allowedClients": cognito_allowed_clients,
                },
            },
            protocol_configuration={
                "mcp": {"supportedVersions": ["2025-03-26", "2025-06-18"]},
            },
            interceptor_configurations=interceptor_configurations,
        )

        # Allow the Gateway to invoke the interceptor Lambda (after gateway ARN is known).
        if self._interceptor_fn is not None:
            self._interceptor_fn.add_permission("AgentCoreInvokeInterceptor",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                source_arn=self._gateway.attr_gateway_arn,
            )

        # Lambda tool role
        lambda_role = iam.Role(self, "LambdaToolRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )

        # Deploy Lambda tools and register as gateway targets
        for tool_name, config in (tool_configs or {}).items():
            fn = _lambda.Function(self, f"Tool-{tool_name}",
                function_name=f"{prefix}-tool-{tool_name}",
                runtime=_lambda.Runtime.PYTHON_3_13,
                handler="handler.handler",
                code=_lambda.Code.from_asset(config["source_dir"]),
                architecture=_lambda.Architecture.ARM_64,
                timeout=cdk.Duration.seconds(30),
                role=lambda_role,
                environment=config.get("env_vars", {}),
            )
            fn.add_permission(f"AgentCoreInvoke-{tool_name}",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                source_arn=self._gateway.attr_gateway_arn,
            )

            target = agentcore.CfnGatewayTarget(self, f"Target-{tool_name}",
                name=tool_name,
                gateway_identifier=self._gateway.attr_gateway_identifier,
                credential_provider_configurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                ],
                target_configuration={
                    "mcp": {},
                },
            )
            # Override the target configuration directly — CDK strips the "lambda" key
            # because it's a Python reserved word and the L1 property mapping doesn't handle it
            target.add_property_override("TargetConfiguration.Mcp.Lambda", {
                "LambdaArn": fn.function_arn,
                "ToolSchema": {
                    "InlinePayload": config.get("tool_schema", []),
                },
            })

        # SSM
        ssm.StringParameter(self, "SSMGatewayUrl",
            parameter_name=f"/{project_name}/{environment}/gateway/url",
            string_value=self._gateway.attr_gateway_url,
        )

        cdk.CfnOutput(self, "GatewayUrl", value=self._gateway.attr_gateway_url)
        cdk.CfnOutput(self, "GatewayArn", value=self._gateway.attr_gateway_arn)
        cdk.CfnOutput(self, "GatewayId", value=self._gateway.attr_gateway_identifier)

    @property
    def gateway_url(self) -> str:
        return self._gateway.attr_gateway_url

    @property
    def gateway_arn(self) -> str:
        return self._gateway.attr_gateway_arn
