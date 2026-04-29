"""Auth Stack — Cognito User Pool with federated IdP support (Entra ID, Okta, Ping Identity).

Implements Requirement 5: Identity Provider Integration
- Cognito User Pool with email sign-in, password policy
- Resource server with `agentcore/invoke` scope
- 3 OAuth clients: app (auth code + PKCE), web (SRP), m2m (client_credentials)
- Optional federated IdP (Entra ID, Okta, Ping) via OIDC provider in Cognito
- SSM Parameters published for cross-stack consumption
"""
import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito, aws_ssm as ssm
from constructs import Construct


class AuthStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        idp_type: str = "cognito",
        idp_config: dict | None = None,
        callback_urls: list[str] | None = None,
        logout_urls: list[str] | None = None,
        **kwargs,
    ):
        """
        Args:
            idp_type: One of 'cognito', 'entra_id', 'okta', 'ping'
            idp_config: IdP-specific settings:
                - entra_id: {tenant_id, client_id, client_secret}
                - okta: {issuer_url, client_id, client_secret}
                - ping: {issuer_url, client_id, client_secret}
        """
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        idp_config = idp_config or {}
        callback_urls = callback_urls or ["http://localhost:3000/api/auth/callback/cognito"]
        logout_urls = logout_urls or ["http://localhost:3000"]

        # ── User Pool ──
        self.user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name=f"{prefix}-user-pool",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── User Pool Domain ──
        domain = self.user_pool.add_domain("Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{prefix}-{self.account}",
            ),
        )

        # ── Federated Identity Provider (optional) ──
        federated_provider = None
        provider_name = "COGNITO"

        if idp_type == "entra_id" and idp_config.get("tenant_id"):
            tenant_id = idp_config["tenant_id"]
            federated_provider = cognito.UserPoolIdentityProviderOidc(self, "EntraIdProvider",
                user_pool=self.user_pool,
                name="EntraID",
                client_id=idp_config["client_id"],
                client_secret=idp_config["client_secret"],
                issuer_url=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email"),
                    fullname=cognito.ProviderAttribute.other("name"),
                ),
            )
            provider_name = "EntraID"

        elif idp_type == "okta" and idp_config.get("issuer_url"):
            federated_provider = cognito.UserPoolIdentityProviderOidc(self, "OktaProvider",
                user_pool=self.user_pool,
                name="Okta",
                client_id=idp_config["client_id"],
                client_secret=idp_config["client_secret"],
                issuer_url=idp_config["issuer_url"],
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email"),
                    fullname=cognito.ProviderAttribute.other("name"),
                ),
            )
            provider_name = "Okta"

        elif idp_type == "ping" and idp_config.get("issuer_url"):
            federated_provider = cognito.UserPoolIdentityProviderOidc(self, "PingProvider",
                user_pool=self.user_pool,
                name="PingIdentity",
                client_id=idp_config["client_id"],
                client_secret=idp_config["client_secret"],
                issuer_url=idp_config["issuer_url"],
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email"),
                    fullname=cognito.ProviderAttribute.other("name"),
                ),
            )
            provider_name = "PingIdentity"

        # Determine supported identity providers for app client
        supported_providers = [cognito.UserPoolClientIdentityProvider.COGNITO]
        if federated_provider:
            supported_providers.append(
                cognito.UserPoolClientIdentityProvider.custom(provider_name)
            )

        # ── Resource Server (agentcore/invoke scope) ──
        resource_server = self.user_pool.add_resource_server("AgentCoreRS",
            identifier="agentcore",
            scopes=[cognito.ResourceServerScope(
                scope_name="invoke",
                scope_description="Invoke AgentCore agents and services",
            )],
        )

        # ── App Client (Authorization Code + PKCE) ──
        self._app_client = self.user_pool.add_client("AppClient",
            user_pool_client_name=f"{prefix}-app-client",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            supported_identity_providers=supported_providers,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                    cognito.OAuthScope.custom("agentcore/invoke"),
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
        )
        self._app_client.node.add_dependency(resource_server)
        if federated_provider:
            self._app_client.node.add_dependency(federated_provider)

        # ── Web Client (SRP only, no secret — for browser SPAs) ──
        self._web_client = self.user_pool.add_client("WebClient",
            user_pool_client_name=f"{prefix}-web-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            supported_identity_providers=supported_providers,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(implicit_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
        )
        if federated_provider:
            self._web_client.node.add_dependency(federated_provider)

        # ── M2M Client (client_credentials — service-to-service) ──
        self._m2m_client = self.user_pool.add_client("M2MClient",
            user_pool_client_name=f"{prefix}-m2m-client",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.custom("agentcore/invoke")],
            ),
        )
        self._m2m_client.node.add_dependency(resource_server)

        # ── SSM Parameters (cross-stack / cross-account discovery) ──
        ssm_params = {
            "issuer-url": f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}",
            "user-pool-id": self.user_pool.user_pool_id,
            "app-client-id": self._app_client.user_pool_client_id,
            "web-client-id": self._web_client.user_pool_client_id,
            "m2m-client-id": self._m2m_client.user_pool_client_id,
        }
        for key, value in ssm_params.items():
            ssm.StringParameter(self, f"SSM-{key}",
                parameter_name=f"/{project_name}/{environment}/auth/{key}",
                string_value=value,
                description=f"Auth {key} for {project_name}/{environment}",
            )

        # ── Outputs ──
        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolArn", value=self.user_pool.user_pool_arn)
        cdk.CfnOutput(self, "IssuerUrl", value=self.issuer_url)
        cdk.CfnOutput(self, "DiscoveryUrl", value=self.discovery_url)
        cdk.CfnOutput(self, "AppClientId", value=self._app_client.user_pool_client_id)
        cdk.CfnOutput(self, "WebClientId", value=self._web_client.user_pool_client_id)
        cdk.CfnOutput(self, "M2MClientId", value=self._m2m_client.user_pool_client_id)
        cdk.CfnOutput(self, "DomainUrl",
            value=f"https://{prefix}-{self.account}.auth.{self.region}.amazoncognito.com")
        cdk.CfnOutput(self, "IdPType", value=idp_type)

    @property
    def issuer_url(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}"

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer_url}/.well-known/openid-configuration"

    @property
    def app_client_id(self) -> str:
        return self._app_client.user_pool_client_id

    @property
    def web_client_id(self) -> str:
        return self._web_client.user_pool_client_id

    @property
    def m2m_client_id(self) -> str:
        return self._m2m_client.user_pool_client_id
