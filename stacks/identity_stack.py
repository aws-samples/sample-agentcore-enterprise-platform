"""Identity Stack — AgentCore OAuth2 Credential Providers.

Implements Requirement 5.5-5.7: 3LO OAuth delegation for external services.
Creates OAuth2 credential providers for Google, GitHub, and Notion.
3LO providers are conditionally created — empty client_id means skip.

Also creates the always-on gateway M2M credential provider: agents exchange the
Cognito M2M client credentials (client_credentials grant) for a Gateway access
token via the AgentCore Token Vault (@requires_access_token, auth_flow="M2M").
"""

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class IdentityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        gateway_m2m_client_id: str,
        gateway_m2m_client_secret: cdk.SecretValue,
        cognito_discovery_url: str,
        google_client_id: str = "",
        google_client_secret: str = "",
        github_client_id: str = "",
        github_client_secret: str = "",
        notion_client_id: str = "",
        notion_client_secret: str = "",
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        self._provider_arns: dict[str, str] = {}

        # ── Gateway M2M Provider (always created) ──
        # Agents fetch Gateway access tokens through this provider via
        # @requires_access_token(provider_name=..., auth_flow="M2M"). The name
        # must match the GATEWAY_CREDENTIAL_PROVIDER_NAME env var on runtimes.
        #
        # CustomOauth2ProviderConfigInput.client_secret takes a plain string, so
        # we unwrap the SecretValue with unsafe_unwrap(). Despite the name, this
        # is the accepted pattern here: it renders the CloudFormation TOKEN
        # (Fn::GetAtt on the DescribeUserPoolClient custom resource in the auth
        # stack, imported here via Fn::ImportValue) into the template — NOT the
        # secret value. The actual secret is resolved only at deploy time by
        # CloudFormation, so it never appears in the synthesized template.
        self._gateway_provider_name = f"{prefix}-gateway-m2m"
        m2m_provider = agentcore.CfnOAuth2CredentialProvider(
            self,
            "GatewayM2M",
            name=self._gateway_provider_name,
            credential_provider_vendor="CustomOauth2",
            oauth2_provider_config_input=agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                custom_oauth2_provider_config=agentcore.CfnOAuth2CredentialProvider.CustomOauth2ProviderConfigInputProperty(
                    # Cognito user pools publish OIDC discovery; the Token Vault
                    # resolves the token endpoint from it for the
                    # client_credentials exchange. Scopes are not part of this
                    # schema — Cognito grants all scopes assigned to the M2M
                    # client (agentcore/invoke) when none are requested.
                    oauth_discovery=agentcore.CfnOAuth2CredentialProvider.Oauth2DiscoveryProperty(
                        discovery_url=cognito_discovery_url,
                    ),
                    client_id=gateway_m2m_client_id,
                    client_secret=gateway_m2m_client_secret.unsafe_unwrap(),
                ),
            ),
        )
        self._gateway_provider_arn = m2m_provider.attr_credential_provider_arn

        ssm.StringParameter(
            self,
            "SSM-gateway-credential-provider-name",
            parameter_name=f"/{project_name}/{environment}/identity/gateway-credential-provider-name",
            string_value=self._gateway_provider_name,
            description=f"Gateway M2M credential provider name for {project_name}/{environment}",
        )
        cdk.CfnOutput(
            self,
            "GatewayCredentialProviderName",
            value=self._gateway_provider_name,
        )

        # ── Google OAuth2 Provider ──
        if google_client_id:
            google_provider = agentcore.CfnOAuth2CredentialProvider(
                self,
                "GoogleOAuth",
                name=f"{prefix}-google-oauth",
                credential_provider_vendor="GoogleOauth2",
                oauth2_provider_config_input={
                    "customOAuth2ProviderConfig": {
                        "oauthDiscoveryUrl": "https://accounts.google.com/.well-known/openid-configuration",
                        "clientId": google_client_id,
                        "clientSecret": google_client_secret,
                        "scopes": [
                            "https://www.googleapis.com/auth/calendar",
                            "https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/drive.readonly",
                        ],
                    },
                },
            )
            self._provider_arns["google"] = google_provider.attr_credential_provider_arn

        # ── GitHub OAuth2 Provider ──
        if github_client_id:
            github_provider = agentcore.CfnOAuth2CredentialProvider(
                self,
                "GitHubOAuth",
                name=f"{prefix}-github-oauth",
                credential_provider_vendor="GithubOauth2",
                oauth2_provider_config_input={
                    "customOAuth2ProviderConfig": {
                        "oauthDiscoveryUrl": "https://github.com/.well-known/openid-configuration",
                        "clientId": github_client_id,
                        "clientSecret": github_client_secret,
                        "scopes": ["repo", "read:user", "user:email"],
                    },
                },
            )
            self._provider_arns["github"] = github_provider.attr_credential_provider_arn

        # ── Notion OAuth2 Provider ──
        if notion_client_id:
            notion_provider = agentcore.CfnOAuth2CredentialProvider(
                self,
                "NotionOAuth",
                name=f"{prefix}-notion-oauth",
                credential_provider_vendor="Custom",
                oauth2_provider_config_input={
                    "customOAuth2ProviderConfig": {
                        "oauthDiscoveryUrl": "https://api.notion.com/.well-known/openid-configuration",
                        "clientId": notion_client_id,
                        "clientSecret": notion_client_secret,
                        "scopes": ["read_content", "read_user"],
                    },
                },
            )
            self._provider_arns["notion"] = notion_provider.attr_credential_provider_arn

        # ── SSM Parameters ──
        for provider_name, arn in self._provider_arns.items():
            ssm.StringParameter(
                self,
                f"SSM-{provider_name}",
                parameter_name=f"/{project_name}/{environment}/identity/{provider_name}-provider-arn",
                string_value=arn,
            )

        # ── Outputs ──
        for provider_name, arn in self._provider_arns.items():
            cdk.CfnOutput(self, f"{provider_name.title()}ProviderArn", value=arn)

    @property
    def provider_arns(self) -> dict[str, str]:
        return self._provider_arns

    @property
    def gateway_credential_provider_name(self) -> str:
        """Provider name agents pass as GATEWAY_CREDENTIAL_PROVIDER_NAME."""
        return self._gateway_provider_name

    @property
    def gateway_credential_provider_arn(self) -> str:
        return self._gateway_provider_arn
