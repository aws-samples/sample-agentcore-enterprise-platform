"""Gateway Stack — AgentCore Gateway with Lambda tool targets."""
import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_ssm as ssm,
)
from constructs import Construct


class GatewayStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, project_name: str, environment: str,
                 cognito_issuer_url: str, cognito_allowed_clients: list[str],
                 tool_configs: dict | None = None, **kwargs):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        gw_name = f"{project_name}_{environment}_gateway".replace("-", "_")

        # Gateway IAM role
        gw_role = iam.Role(self, "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            role_name=f"{prefix}-gateway-role",
        )
        gw_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:{prefix}-tool-*"],
        ))

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
                    "allowedAudience": cognito_allowed_clients,
                },
            },
            protocol_configuration={
                "mcp": {"supportedVersions": ["2025-03-26", "2025-06-18"]},
            },
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
                handler="index.handler",
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

            agentcore.CfnGatewayTarget(self, f"Target-{tool_name}",
                name=tool_name,
                gateway_identifier=self._gateway.attr_gateway_identifier,
                credential_provider_configurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                ],
                target_configuration={
                    "mcp": {
                        "lambda_": {
                            "lambdaArn": fn.function_arn,
                            "toolSchema": {
                                "inlinePayload": config.get("tool_schema", []),
                            },
                        },
                    },
                },
            )

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
