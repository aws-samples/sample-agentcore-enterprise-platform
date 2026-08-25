"""3LO credential providers: secrets by name, config that actually renders.

Static guards in the style of test_web_search_target.py (the CI test job has no
aws_cdk, so these parse source). Two defects, both found live:

1. Client secrets were rendered verbatim into the synthesized template
   (customOAuth2ProviderConfig.clientSecret) from -c context / env plaintext.
   Now they travel as Secrets Manager NAMES and render as
   {{resolve:secretsmanager:...}} dynamic references.
2. The provider config was passed as a raw dict whose top-level key
   ("customOAuth2ProviderConfig", capital OA) did not match the CloudFormation
   model — the L1 mapping silently dropped the whole block and the template
   carried Oauth2ProviderConfigInput: {}. The providers could never have
   worked. Typed property classes raise at synth instead.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_SRC = (ROOT / "stacks" / "identity_stack.py").read_text()
APP_SRC = (ROOT / "app.py").read_text()
DEPLOY_SRC = (ROOT / "scripts" / "deploy.sh").read_text()


def test_no_plaintext_secret_parameters():
    # The stack accepts secret NAMES only. A *_client_secret parameter
    # reintroduces the template leak.
    for vendor in ("google", "github", "notion"):
        assert f"{vendor}_client_secret_name" in IDENTITY_SRC
        assert f"{vendor}_client_secret:" not in IDENTITY_SRC  # old param form


def test_secrets_render_as_dynamic_references():
    assert "SecretValue.secrets_manager" in IDENTITY_SRC
    # unsafe_unwrap renders the TOKEN, not the value — same as the auth stack.
    assert "unsafe_unwrap" in IDENTITY_SRC


def test_no_raw_dict_provider_config():
    # The raw-dict key that silently dropped the whole config (quoted = used
    # as a dict key; the comments explaining the defect may name it bare).
    assert '"customOAuth2ProviderConfig"' not in IDENTITY_SRC
    # Typed property classes are the load-bearing replacement.
    for prop in (
        "GoogleOauth2ProviderConfigInputProperty",
        "GithubOauth2ProviderConfigInputProperty",
        "CustomOauth2ProviderConfigInputProperty",
    ):
        assert prop in IDENTITY_SRC


def test_app_rejects_plaintext_and_reads_names():
    assert "no longer supported" in APP_SRC
    for vendor in ("google", "github", "notion"):
        assert f"{vendor}_client_secret_name" in APP_SRC
    # The rejection must cover the env-var spelling too.
    assert "_CLIENT_SECRET" in APP_SRC


def test_deploy_upserts_and_passes_names_only():
    assert "upsert_3lo_secrets" in DEPLOY_SRC
    for vendor in ("GOOGLE", "GITHUB", "NOTION"):
        assert f"{vendor}_CLIENT_SECRET_NAME" in DEPLOY_SRC
        # The plaintext env var must never be a context arg.
        assert f'-c "{vendor.lower()}_client_secret=' not in DEPLOY_SRC
