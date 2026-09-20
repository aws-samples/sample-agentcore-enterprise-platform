"""Auth Stack — Cognito User Pool with federated IdP support (Entra ID, Okta, Ping Identity).

Implements Requirement 5: Identity Provider Integration
- Cognito User Pool with email sign-in, password policy
- Resource server with `agentcore/invoke` scope
- 3 OAuth clients: app (auth code), web (auth code + PKCE), m2m (client_credentials)
- Optional federated IdP (Entra ID, Okta, Ping) via OIDC provider in Cognito
- SSM Parameters published for cross-stack consumption
"""

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
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
        idp_mode: str = "brokered",
        idp_config: dict | None = None,
        callback_urls: list[str] | None = None,
        logout_urls: list[str] | None = None,
        retain_legacy_m2m_client: bool = False,
        publish_legacy_m2m_interface: bool = False,
        **kwargs,
    ):
        """
        Args:
            idp_type: One of 'cognito', 'entra_id', 'okta', 'ping'
            idp_mode: 'brokered' (Cognito issues tokens; the IdP federates
                through it) or 'direct' (the IdP issues tokens; no user pool
                is created and this stack only publishes the issuer facts to
                the same /auth/* SSM interface). See IdentityConfig.
            idp_config: IdP-specific settings:
                - entra_id: {tenant_id, client_id, client_secret_name}
                - okta: {issuer_url, client_id, client_secret_name}
                - ping: {issuer_url, client_id, client_secret_name}
            client_secret_name is the NAME of a Secrets Manager secret holding the
            IdP client secret — never the secret value itself.
        """
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        idp_config = idp_config or {}
        external_idp_types = ("entra_id", "okta", "ping")

        if idp_type not in ("cognito", *external_idp_types):
            raise ValueError(
                "idp_type must be one of 'cognito', 'entra_id', 'okta', or 'ping'"
            )

        # Federated IdPs require the client secret via Secrets Manager — fail fast
        # at synth time with an actionable message instead of a mid-deploy error.
        if idp_type in external_idp_types and not idp_config.get("client_secret_name"):
            raise ValueError(
                f"idp_type='{idp_type}' requires the context key 'idp_client_secret_name' "
                "(the name of a Secrets Manager secret containing the IdP client secret). "
                "Store the secret first, e.g.:\n"
                f"  aws secretsmanager create-secret --name {prefix}-idp-client-secret "
                "--secret-string '<client-secret>'\n"
                f"then pass: -c idp_client_secret_name={prefix}-idp-client-secret\n"
                "(scripts/deploy.sh does both automatically when prompted for the IdP secret). "
                "The plaintext 'idp_client_secret' context key is no longer supported."
            )
        if idp_type in external_idp_types:
            required_fields = ["client_id"]
            required_fields.append(
                "tenant_id" if idp_type == "entra_id" else "issuer_url"
            )
            missing_fields = [
                field for field in required_fields if not idp_config.get(field)
            ]
            if missing_fields:
                missing = ", ".join(f"idp_{field}" for field in missing_fields)
                raise ValueError(
                    f"idp_type='{idp_type}' requires the context key(s): {missing}"
                )
        callback_urls = callback_urls or [
            "http://localhost:3000/api/auth/callback/cognito"
        ]
        logout_urls = logout_urls or ["http://localhost:3000"]

        self._mode = idp_mode
        if idp_mode == "direct":
            self._build_direct(project_name, environment, idp_type, idp_config)
            return

        has_external_idp = idp_type in external_idp_types

        # ── User Pool ──
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{prefix}-user-pool",
            # Corporate users must enter through the configured IdP. The
            # Cognito-only workshop path keeps its self-service signup flow.
            self_sign_up_enabled=not has_external_idp,
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
        self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{prefix}-{self.account}",
            ),
        )

        # ── Federated Identity Provider (optional) ──
        federated_provider = None
        provider_name = "COGNITO"

        # UserPoolIdentityProviderOidc.client_secret takes a plain string, so we
        # unwrap the SecretValue with unsafe_unwrap(). Despite the name, this is
        # the accepted pattern here: it renders the {{resolve:secretsmanager:...}}
        # dynamic-reference TOKEN into the template — NOT the secret value. The
        # actual secret is resolved only at deploy time by CloudFormation, so it
        # never appears in the synthesized template, cdk.out, or process args.
        idp_client_secret = None
        if has_external_idp:
            idp_client_secret = cdk.SecretValue.secrets_manager(
                idp_config["client_secret_name"]
            ).unsafe_unwrap()

        if idp_type == "entra_id" and idp_config.get("tenant_id"):
            tenant_id = idp_config["tenant_id"]
            federated_provider = cognito.UserPoolIdentityProviderOidc(
                self,
                "EntraIdProvider",
                user_pool=self.user_pool,
                name="EntraID",
                client_id=idp_config["client_id"],
                client_secret=idp_client_secret,
                issuer_url=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email"),
                    fullname=cognito.ProviderAttribute.other("name"),
                ),
            )
            provider_name = "EntraID"

        elif idp_type == "okta" and idp_config.get("issuer_url"):
            federated_provider = cognito.UserPoolIdentityProviderOidc(
                self,
                "OktaProvider",
                user_pool=self.user_pool,
                name="Okta",
                client_id=idp_config["client_id"],
                client_secret=idp_client_secret,
                issuer_url=idp_config["issuer_url"],
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email"),
                    fullname=cognito.ProviderAttribute.other("name"),
                ),
            )
            provider_name = "Okta"

        elif idp_type == "ping" and idp_config.get("issuer_url"):
            federated_provider = cognito.UserPoolIdentityProviderOidc(
                self,
                "PingProvider",
                user_pool=self.user_pool,
                name="PingIdentity",
                client_id=idp_config["client_id"],
                client_secret=idp_client_secret,
                issuer_url=idp_config["issuer_url"],
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other("email"),
                    fullname=cognito.ProviderAttribute.other("name"),
                ),
            )
            provider_name = "PingIdentity"

        # A brokered corporate IdP is the only interactive user provider. Keeping
        # COGNITO here would expose a second sign-in path that bypasses the
        # corporate IdP's MFA and conditional-access policy.
        supported_providers = (
            [cognito.UserPoolClientIdentityProvider.custom(provider_name)]
            if has_external_idp
            else [cognito.UserPoolClientIdentityProvider.COGNITO]
        )

        # ── Resource Server (agentcore/invoke scope) ──
        resource_server = self.user_pool.add_resource_server(
            "AgentCoreRS",
            identifier="agentcore",
            scopes=[
                cognito.ResourceServerScope(
                    scope_name="invoke",
                    scope_description="Invoke AgentCore agents and services",
                )
            ],
        )

        # ── App Client (Authorization Code + PKCE) ──
        self._app_client = self.user_pool.add_client(
            "AppClient",
            user_pool_client_name=f"{prefix}-app-client",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(
                user_password=not has_external_idp,
                user_srp=not has_external_idp,
            ),
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

        # ── Web Client (Authorization Code + PKCE, no secret — browser SPA) ──
        self._web_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name=f"{prefix}-web-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=not has_external_idp),
            supported_identity_providers=supported_providers,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
        )
        if federated_provider:
            self._web_client.node.add_dependency(federated_provider)

        # ── M2M Client (client_credentials — service-to-service) ──
        #
        # M2MClientV2 intentionally has a new logical id. G0 rotates the client
        # whose credential was published by the old dashboard. During an
        # upgrade deploy.sh temporarily asks this stack to retain the original
        # client and secret so consumers can move without an outage.
        self._m2m_client = self.user_pool.add_client(
            "M2MClientV2",
            user_pool_client_name=f"{prefix}-m2m-client-v2",
            generate_secret=True,
            access_token_validity=cdk.Duration.minutes(60),
            auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.custom("agentcore/invoke")],
            ),
        )
        self._m2m_client.node.add_dependency(resource_server)

        # Keep the generated client secret inside this stack. Passing
        # user_pool_client_secret directly to IdentityStack makes CDK export
        # the secret-bearing Fn::GetAtt through CloudFormation. Store it here
        # and let consumers resolve it by secret name instead.
        self._m2m_client_secret_store = secretsmanager.Secret(
            self,
            "M2MClientSecretV2",
            description=f"Gateway M2M client secret for {project_name}/{environment}",
            secret_string_value=self._m2m_client.user_pool_client_secret,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self._m2m_client_secret_store.node.add_dependency(self._m2m_client)
        # Keep this non-sensitive reference explicit and stable. A normal CDK
        # cross-stack reference is lazy, so the compatibility synthesis omitted
        # it while Identity still consumed the old value and phase 2 had
        # nothing to import.
        self._m2m_client_id_export_name = f"{prefix}:auth:m2m-client-id-v2"
        self._m2m_client_secret_export_name = f"{prefix}:auth:m2m-client-secret-name-v2"
        cdk.CfnOutput(
            self,
            "M2MClientIdV2Export",
            value=self._m2m_client.user_pool_client_id,
            export_name=self._m2m_client_id_export_name,
        )
        cdk.CfnOutput(
            self,
            "M2MClientSecretNameV2Export",
            value=self._m2m_client_secret_store.secret_name,
            export_name=self._m2m_client_secret_export_name,
        )

        if retain_legacy_m2m_client:
            self._legacy_m2m_client = self.user_pool.add_client(
                "M2MClient",
                user_pool_client_name=f"{prefix}-m2m-client",
                generate_secret=True,
                access_token_validity=cdk.Duration.minutes(60),
                auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(client_credentials=True),
                    scopes=[cognito.OAuthScope.custom("agentcore/invoke")],
                ),
            )
            self._legacy_m2m_client.node.add_dependency(resource_server)
            self._legacy_m2m_client_secret_store = secretsmanager.Secret(
                self,
                "M2MClientSecret",
                description=(
                    f"Legacy gateway M2M client secret for "
                    f"{project_name}/{environment}; removed after G0 rotation"
                ),
                secret_string_value=self._legacy_m2m_client.user_pool_client_secret,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            self._legacy_m2m_client_secret_store.node.add_dependency(
                self._legacy_m2m_client
            )
            self._legacy_m2m_client_secret_export_name = (
                f"{prefix}:auth:m2m-client-secret-name"
            )
            cdk.CfnOutput(
                self,
                "M2MClientSecretNameExport",
                value=self._legacy_m2m_client_secret_store.secret_name,
                export_name=self._legacy_m2m_client_secret_export_name,
            )

        publish_legacy_m2m_interface = (
            publish_legacy_m2m_interface and retain_legacy_m2m_client
        )
        published_m2m_client = (
            self._legacy_m2m_client
            if publish_legacy_m2m_interface
            else self._m2m_client
        )
        published_m2m_secret_store = (
            self._legacy_m2m_client_secret_store
            if publish_legacy_m2m_interface
            else self._m2m_client_secret_store
        )

        # ── SSM Parameters (cross-stack / cross-account discovery) ──
        self._publish(
            project_name,
            environment,
            {
                "mode": "brokered",
                "issuer-url": f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}",
                "user-pool-id": self.user_pool.user_pool_id,
                "app-client-id": self._app_client.user_pool_client_id,
                "web-client-id": self._web_client.user_pool_client_id,
                "m2m-client-id": published_m2m_client.user_pool_client_id,
                "m2m-client-secret-name": published_m2m_secret_store.secret_name,
                "m2m-scope": "agentcore/invoke",
            },
        )

        # ── Outputs ──
        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolArn", value=self.user_pool.user_pool_arn)
        cdk.CfnOutput(self, "IssuerUrl", value=self.issuer_url)
        cdk.CfnOutput(self, "DiscoveryUrl", value=self.discovery_url)
        cdk.CfnOutput(self, "AppClientId", value=self._app_client.user_pool_client_id)
        cdk.CfnOutput(self, "WebClientId", value=self._web_client.user_pool_client_id)
        cdk.CfnOutput(
            self,
            "M2MClientId",
            value=published_m2m_client.user_pool_client_id,
        )
        cdk.CfnOutput(
            self,
            "DomainUrl",
            value=f"https://{prefix}-{self.account}.auth.{self.region}.amazoncognito.com",
        )
        cdk.CfnOutput(self, "IdPType", value=idp_type)
        cdk.CfnOutput(self, "IdPMode", value="brokered")

    def _build_direct(
        self, project_name: str, environment: str, idp_type: str, idp_config: dict
    ) -> None:
        """Direct mode: the IdP is the issuer. Nothing to create — this stack
        exists so the /auth/* interface (and the contract) stay the same shape
        for every consumer. The M2M credential is the IdP app's own client
        secret, already in Secrets Manager under client_secret_name for the
        brokered path; direct mode just reads it for a different purpose.
        Only Entra is wired today (IdentityConfig enforces this)."""
        if idp_type != "entra_id":
            raise ValueError("idp_mode='direct' supports idp_type 'entra_id' only")
        for key in ("tenant_id", "client_id", "client_secret_name"):
            if not idp_config.get(key):
                raise ValueError(f"idp_mode='direct' requires idp_{key}")
        tenant_id = idp_config["tenant_id"]
        self._direct_issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._direct_client_id = idp_config["client_id"]
        self._direct_secret_name = idp_config["client_secret_name"]
        self._publish(
            project_name,
            environment,
            {
                "mode": "direct",
                "issuer-url": self._direct_issuer,
                # The same app registration serves humans (auth-code) and
                # machines (client_credentials); there is no separate M2M app.
                "app-client-id": self._direct_client_id,
                "m2m-client-id": self._direct_client_id,
                "m2m-client-secret-name": idp_config["client_secret_name"],
                "m2m-scope": f"{self._direct_client_id}/.default",
            },
        )
        cdk.CfnOutput(self, "IssuerUrl", value=self.issuer_url)
        cdk.CfnOutput(self, "DiscoveryUrl", value=self.discovery_url)
        cdk.CfnOutput(self, "AppClientId", value=self._direct_client_id)
        cdk.CfnOutput(self, "M2MClientId", value=self._direct_client_id)
        cdk.CfnOutput(self, "IdPType", value=idp_type)
        cdk.CfnOutput(self, "IdPMode", value="direct")

    def _publish(self, project_name: str, environment: str, params: dict) -> None:
        for key, value in params.items():
            ssm.StringParameter(
                self,
                f"SSM-{key}",
                parameter_name=f"/{project_name}/{environment}/auth/{key}",
                string_value=value,
                description=f"Auth {key} for {project_name}/{environment}",
            )

    @property
    def is_direct(self) -> bool:
        return self._mode == "direct"

    @property
    def issuer_url(self) -> str:
        if self.is_direct:
            return self._direct_issuer
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}"

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer_url}/.well-known/openid-configuration"

    @property
    def app_client_id(self) -> str:
        if self.is_direct:
            return self._direct_client_id
        return self._app_client.user_pool_client_id

    @property
    def web_client_id(self) -> str:
        return self._web_client.user_pool_client_id

    @property
    def m2m_client_id(self) -> str:
        if self.is_direct:
            return self._direct_client_id
        return cdk.Fn.import_value(self._m2m_client_id_export_name)

    @property
    def m2m_client_secret_name(self) -> str:
        """Secrets Manager name consumed by the account-local token vault."""
        if self.is_direct:
            return self._direct_secret_name
        return cdk.Fn.import_value(self._m2m_client_secret_export_name)

    @property
    def legacy_m2m_client_id(self) -> str:
        """Upgrade-only id for the client being invalidated by G0."""
        if self.is_direct or not hasattr(self, "_legacy_m2m_client"):
            raise ValueError("no legacy Cognito M2M client is retained")
        return self._legacy_m2m_client.user_pool_client_id

    @property
    def legacy_m2m_client_secret_name(self) -> str:
        """Upgrade-only managed-secret reference for the old client."""
        if self.is_direct or not hasattr(self, "_legacy_m2m_client_secret_export_name"):
            raise ValueError("no legacy Cognito M2M client is retained")
        return cdk.Fn.import_value(self._legacy_m2m_client_secret_export_name)

    @property
    def legacy_m2m_client_secret(self) -> cdk.SecretValue:
        """Upgrade-only value that preserves the pre-G0 CDK export.

        app.py reads this only under the internal
        retain_legacy_m2m_export context flag used by deploy.sh's staged
        migration. Normal synthesis must use m2m_client_secret_name.
        """
        if self.is_direct or not hasattr(self, "_legacy_m2m_client"):
            raise ValueError("no legacy Cognito M2M client is retained")
        return self._legacy_m2m_client.user_pool_client_secret
