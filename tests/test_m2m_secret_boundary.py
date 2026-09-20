"""The generated Cognito M2M secret must not cross a stack output boundary."""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk

from stacks.auth_stack import AuthStack
from stacks.identity_stack import IdentityStack


def _synthesized_templates(tmp_path, *, retain_legacy_export=False):
    app = cdk.App(outdir=str(tmp_path))
    env = cdk.Environment(account="111111111111", region="us-east-1")
    auth = AuthStack(
        app,
        "test-auth",
        project_name="test-platform",
        environment="test",
        retain_legacy_m2m_client=retain_legacy_export,
        publish_legacy_m2m_interface=retain_legacy_export,
        env=env,
    )
    identity = IdentityStack(
        app,
        "test-identity",
        project_name="test-platform",
        environment="test",
        gateway_m2m_client_id=auth.m2m_client_id,
        gateway_m2m_client_secret_name=auth.m2m_client_secret_name,
        gateway_m2m_client_secret=(
            auth.legacy_m2m_client_secret if retain_legacy_export else None
        ),
        cognito_discovery_url=auth.discovery_url,
        env=env,
    )
    identity.add_dependency(auth)

    assembly = app.synth()
    return (
        assembly.get_stack_by_name("test-auth").template,
        assembly.get_stack_by_name("test-identity").template,
    )


def test_generated_m2m_secret_is_stored_in_secrets_manager(tmp_path):
    auth_template, _ = _synthesized_templates(tmp_path)

    secrets = [
        resource
        for resource in auth_template["Resources"].values()
        if resource["Type"] == "AWS::SecretsManager::Secret"
    ]
    assert len(secrets) == 1
    assert "UserPoolClient.ClientSecret" in json.dumps(secrets[0]["Properties"])


def test_secret_value_is_not_exported_or_imported(tmp_path):
    auth_template, identity_template = _synthesized_templates(tmp_path)

    exported_values = json.dumps(
        [output["Value"] for output in auth_template.get("Outputs", {}).values()]
    )
    assert "UserPoolClient.ClientSecret" not in exported_values

    identity_json = json.dumps(identity_template)
    assert "resolve:secretsmanager" in identity_json
    assert "UserPoolClient.ClientSecret" not in identity_json


def test_upgrade_compatibility_synthesis_retains_old_export_temporarily(tmp_path):
    auth_template, identity_template = _synthesized_templates(
        tmp_path, retain_legacy_export=True
    )

    exported_values = json.dumps(
        [output["Value"] for output in auth_template.get("Outputs", {}).values()]
    )
    assert "UserPoolClient.ClientSecret" in exported_values
    assert any("M2MClientSecret" in key for key in auth_template.get("Outputs", {}))
    provider = next(
        resource
        for resource in identity_template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::OAuth2CredentialProvider"
    )
    client_secret = provider["Properties"]["Oauth2ProviderConfigInput"][
        "CustomOauth2ProviderConfig"
    ]["ClientSecret"]
    assert "UserPoolClientClientSecret" in client_secret["Fn::ImportValue"]


def test_normal_synthesis_contains_only_the_rotated_m2m_client(tmp_path):
    auth_template, _ = _synthesized_templates(tmp_path)
    clients = {
        resource["Properties"]["ClientName"]: resource["Properties"]
        for resource in auth_template["Resources"].values()
        if resource["Type"] == "AWS::Cognito::UserPoolClient"
    }

    assert "test-platform-test-m2m-client-v2" in clients
    assert "test-platform-test-m2m-client" not in clients
    replacement = clients["test-platform-test-m2m-client-v2"]
    assert replacement["AccessTokenValidity"] == 60
    assert replacement["TokenValidityUnits"]["AccessToken"] == "minutes"


def test_compatibility_synthesis_keeps_old_and_new_m2m_clients(tmp_path):
    auth_template, _ = _synthesized_templates(tmp_path, retain_legacy_export=True)
    client_names = {
        resource["Properties"]["ClientName"]
        for resource in auth_template["Resources"].values()
        if resource["Type"] == "AWS::Cognito::UserPoolClient"
    }

    assert "test-platform-test-m2m-client-v2" in client_names
    assert "test-platform-test-m2m-client" in client_names


def test_deploy_script_orders_and_scopes_legacy_migration():
    script = (Path(__file__).parents[1] / "scripts" / "deploy.sh").read_text()

    assert 'if [ "${IDP_MODE:-brokered}" != "brokered" ]; then' in script
    assert script.index("Migration 1/3") < script.index("Migration 2/3")
    assert script.index("Migration 2/3") < script.index("Migration 3/3")
    assert "-c retain_legacy_m2m_export=true" in script
    assert "| redact_legacy_m2m_output" in script
    for phase in range(1, 7):
        assert f"Rotation {phase}/6" in script
    assert "retired_m2m_client_id=" in script
    assert "-c transition_m2m_consumers=true" in script
    assert script.index("Rotation 1/6") < script.index("Rotation 2/6")
    assert script.index("Rotation 2/6") < script.index("Rotation 3/6")
    rotation_1 = script.split("Rotation 1/6", 1)[1].split("Rotation 2/6", 1)[0]
    rotation_2 = script.split("Rotation 2/6", 1)[1].split("Rotation 3/6", 1)[0]
    rotation_3 = script.split("Rotation 3/6", 1)[1].split("Rotation 4/6", 1)[0]
    rotation_5 = script.split("Rotation 5/6", 1)[1]
    assert "retired_m2m_client_id=" in rotation_1
    assert '"${PREFIX}-identity"' not in rotation_1
    assert "retain_legacy_m2m_client=true" in rotation_2
    assert '"${PREFIX}-identity"' in rotation_3
    assert "transition_m2m_consumers=true" in rotation_3
    assert rotation_5.index('put-parameter --name "$retired_id_parameter"') < (
        rotation_5.index('npx cdk deploy "${PREFIX}-auth"')
    )
    assert "drain_seconds=3900" in script
    assert 'if [ "$now" -lt "$not_before" ]' in script
    assert "M2M_ROTATION_CONTEXT_ARGS=(" in script
    assert script.count("m2m-retired-client-id") >= 2
    assert "read_optional_rotation_parameter" in script
    assert "aws ssm get-parameters" in script
    assert "!contains(OutputKey, 'UserPoolM2MClientV2')" in script
    assert "Could not clear M2M rotation checkpoints for direct mode." in script
    assert 'DEPLOYMENT_STRATEGY:-centralized}" = "federated"' in script
    assert "FEDERATED_M2M_CONSUMERS_UPDATED" in script
    assert "M2MClientIdV2Export and M2MClientSecretNameV2Export" in script
    gate = script.index(
        'log_error "Federated M2M rotation now requires a coordinated workload update."'
    )
    pre_stage = script.index(
        "Federated rotation preparation: allow both clients on platform authorizers"
    )
    assert pre_stage < gate
    assert "acquire_m2m_deployment_lock" in script
    assert "--no-overwrite" in script
    assert "release_m2m_deployment_lock" in script
    assert "Removing an expired deployment lock" not in script
    assert "must review and delete" in script


def test_migration_requires_safe_v2_exports_after_partial_rollback():
    script = (Path(__file__).parents[1] / "scripts" / "deploy.sh").read_text()
    migration = script.split("migrate_legacy_m2m_secret_export() {", 1)[1].split(
        "rotate_legacy_m2m_client() {", 1
    )[0]

    assert "replacement_export_count" in migration
    assert "'${PREFIX}:auth:m2m-client-id-v2'" in migration
    assert "'${PREFIX}:auth:m2m-client-secret-name-v2'" in migration
    assert (
        '[ "$legacy_count" = "0" ] && [ "$replacement_export_count" = "2" ]'
        in migration
    )
    assert "its M2M migration state could not be read" in migration


def test_federated_workloads_can_drain_the_previous_client_id():
    root = Path(__file__).parents[1]
    app = (root / "app.py").read_text()
    multi_account = (root / "docs" / "MULTI_ACCOUNT.md").read_text()
    workload_wiring = app.split("if is_fed_workload:", 1)[1].split(
        "# Omitted when empty",
        1,
    )[0]

    deploy = (root / "scripts" / "deploy.sh").read_text()
    assert "retired_m2m_client_id=${FEDERATED_RETIRED_M2M_CLIENT_ID}" in deploy
    assert "elif retired_m2m_client_id:" in workload_wiring
    assert "allowed_clients.append(retired_m2m_client_id)" in workload_wiring
    assert "FEDERATED_RETIRED_M2M_CLIENT_ID=<previous-client-id>" in multi_account
    assert "without `FEDERATED_RETIRED_M2M_CLIENT_ID`" in multi_account


def test_live_update_docs_use_the_upgrade_aware_entry_point():
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text()
    security = (root / "docs" / "SECURITY_CONTROLS.md").read_text()
    testing = (root / "docs" / "TESTING.md").read_text()

    assert "Direct `cdk deploy` is supported only for disposable test stacks" in (
        " ".join(readme.split())
    )
    app = (root / "app.py").read_text()
    assert "cdk deploy agentcore-" not in app
    assert "cdk deploy --all" not in app
    assert "cdk deploy -c" not in app
    assert "cdk deploy agentcore-workshop-dev-gateway" not in security
    assert "cdk deploy agentcore-workshop-dev-gateway" not in testing


def test_export_and_summary_reuse_dashboard_public_allowlist():
    root = Path(__file__).parents[1]
    script = (root / "scripts" / "deploy.sh").read_text()
    gitignore = (root / ".gitignore").read_text()
    export_body = script.split("export_artifacts() {", 1)[1].split(
        "# ═══════════════════════════════════════════════════════════════\n# Main",
        1,
    )[0]
    summary_body = script.split("print_summary() {", 1)[1].split("\n}", 1)[0]

    assert "sanitize_public_metadata ssm" in export_body
    assert "sanitize_public_metadata stack-export" in export_body
    assert "sanitize_public_metadata stack" in summary_body
    assert "acquire_m2m_deployment_lock" in export_body
    assert "release_m2m_deployment_lock" in export_body
    assert "os.chmod(temporary, 0o600)" in export_body
    assert "workshop-outputs-*.json" in gitignore


def test_every_targeted_deploy_runs_upgrade_guards_before_its_stack_loop():
    script = (Path(__file__).parents[1] / "scripts" / "deploy.sh").read_text()
    body = script.split("deploy_stacks() {", 1)[1].split("\n}", 1)[0]

    loop_at = body.index('for stack in "$@"; do')
    for guard in (
        "switch_issuer_consumers_first",
        "migrate_legacy_m2m_secret_export",
        "rotate_legacy_m2m_client",
    ):
        assert body.index(guard) < loop_at
