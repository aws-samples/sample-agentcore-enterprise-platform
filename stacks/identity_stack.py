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
        google_client_secret_name: str = "",
        github_client_id: str = "",
        github_client_secret_name: str = "",
        notion_client_id: str = "",
        notion_client_secret_name: str = "",
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        self._provider_arns: dict[str, str] = {}

        # 3LO secrets arrive as Secrets Manager secret NAMES, never values —
        # the plaintext used to be rendered verbatim into
        # customOAuth2ProviderConfig.clientSecret in the synthesized template.
        # Same fail-fast contract as the auth stack's IdP secret.
        for vendor, cid, secret_name in (
            ("google", google_client_id, google_client_secret_name),
            ("github", github_client_id, github_client_secret_name),
            ("notion", notion_client_id, notion_client_secret_name),
        ):
            if cid and not secret_name:
                raise ValueError(
                    f"{vendor}_client_id is set but '{vendor}_client_secret_name' is missing "
                    "(the name of a Secrets Manager secret holding the OAuth client secret). "
                    "Store the secret first, e.g.:\n"
                    f"  aws secretsmanager create-secret --name {prefix}-{vendor}-oauth-secret "
                    "--secret-string '<client-secret>'\n"
                    f"then pass: -c {vendor}_client_secret_name={prefix}-{vendor}-oauth-secret\n"
                    "(scripts/deploy.sh does both automatically when the secret is in the "
                    f"environment). The plaintext '{vendor}_client_secret' key is no longer "
                    "supported."
                )

        def _resolve(secret_name: str) -> str:
            # Renders a {{resolve:secretsmanager:...}} dynamic-reference TOKEN
            # into the template — not the value. CloudFormation resolves it at
            # deploy time, so the secret never appears in cdk.out or the
            # synthesized template. Same pattern as the auth stack.
            return cdk.SecretValue.secrets_manager(secret_name).unsafe_unwrap()

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

        # ── 3LO Providers (Google / GitHub / Notion) ──
        # Typed L1 property classes, deliberately: these blocks used to pass a
        # raw dict whose top-level key spelled OAuth with a capital A (the
        # model wants Oauth), and the L1 mapping silently dropped the ENTIRE
        # config — the template carried
        # Oauth2ProviderConfigInput: {}. Same hazard class as the web-search
        # connector (docs/GATEWAY_TARGETS.md). The typed classes raise at synth
        # on a wrong key instead. Scopes are not provider config: agents
        # request them per token via @requires_access_token(scopes=[...]).
        _P = agentcore.CfnOAuth2CredentialProvider

        # Google and GitHub are vendor-known: the Token Vault knows their
        # endpoints, so config is just the client pair.
        if google_client_id:
            google_provider = _P(
                self,
                "GoogleOAuth",
                name=f"{prefix}-google-oauth",
                credential_provider_vendor="GoogleOauth2",
                oauth2_provider_config_input=_P.Oauth2ProviderConfigInputProperty(
                    google_oauth2_provider_config=_P.GoogleOauth2ProviderConfigInputProperty(
                        client_id=google_client_id,
                        client_secret=_resolve(google_client_secret_name),
                    ),
                ),
            )
            self._provider_arns["google"] = google_provider.attr_credential_provider_arn

        if github_client_id:
            github_provider = _P(
                self,
                "GitHubOAuth",
                name=f"{prefix}-github-oauth",
                credential_provider_vendor="GithubOauth2",
                oauth2_provider_config_input=_P.Oauth2ProviderConfigInputProperty(
                    github_oauth2_provider_config=_P.GithubOauth2ProviderConfigInputProperty(
                        client_id=github_client_id,
                        client_secret=_resolve(github_client_secret_name),
                    ),
                ),
            )
            self._provider_arns["github"] = github_provider.attr_credential_provider_arn

        # Notion has no vendor config — CustomOauth2 with explicit endpoints,
        # the same shape as the (deployed and working) gateway M2M provider.
        # Notion publishes no OIDC discovery document, so the endpoints are
        # spelled out instead of discovered.
        if notion_client_id:
            notion_provider = _P(
                self,
                "NotionOAuth",
                name=f"{prefix}-notion-oauth",
                credential_provider_vendor="CustomOauth2",
                oauth2_provider_config_input=_P.Oauth2ProviderConfigInputProperty(
                    custom_oauth2_provider_config=_P.CustomOauth2ProviderConfigInputProperty(
                        oauth_discovery=_P.Oauth2DiscoveryProperty(
                            authorization_server_metadata=_P.Oauth2AuthorizationServerMetadataProperty(
                                issuer="https://api.notion.com",
                                authorization_endpoint="https://api.notion.com/v1/oauth/authorize",
                                token_endpoint="https://api.notion.com/v1/oauth/token",
                            ),
                        ),
                        client_id=notion_client_id,
                        client_secret=_resolve(notion_client_secret_name),
                    ),
                ),
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
