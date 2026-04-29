"""Identity Stack — AgentCore OAuth2 Credential Providers for 3LO delegation.

Implements Requirement 5.5-5.7: 3LO OAuth delegation for external services.
Creates OAuth2 credential providers for Google, GitHub, and Notion.
Providers are conditionally created — empty client_id means skip.
"""
import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore, aws_ssm as ssm
from constructs import Construct


class IdentityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
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

        # ── Google OAuth2 Provider ──
        if google_client_id:
            google_provider = agentcore.CfnOAuth2CredentialProvider(self, "GoogleOAuth",
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
            github_provider = agentcore.CfnOAuth2CredentialProvider(self, "GitHubOAuth",
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
            notion_provider = agentcore.CfnOAuth2CredentialProvider(self, "NotionOAuth",
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
            ssm.StringParameter(self, f"SSM-{provider_name}",
                parameter_name=f"/{project_name}/{environment}/identity/{provider_name}-provider-arn",
                string_value=arn,
            )

        # ── Outputs ──
        for provider_name, arn in self._provider_arns.items():
            cdk.CfnOutput(self, f"{provider_name.title()}ProviderArn", value=arn)

    @property
    def provider_arns(self) -> dict[str, str]:
        return self._provider_arns
