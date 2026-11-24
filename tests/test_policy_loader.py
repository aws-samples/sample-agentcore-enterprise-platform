"""Unit tests for infra_utils.policy_loader."""

import json

import pytest

from infra_utils.policy_loader import (
    load_catalog,
    load_control,
    load_control_json,
    load_control_text,
)

# ── Against the real control-library ──


def test_catalog_lists_seeded_controls():
    catalog = load_catalog()
    ids = {c["id"] for c in catalog["controls"]}
    assert "scp.memory.enforce-cmk" in ids
    assert "resource-policy.memory.in-account-only" in ids


def test_scp_uses_default_when_param_omitted():
    doc = load_control("scp.memory.enforce-cmk")
    pattern = doc["Statement"][0]["Condition"]["StringNotLike"][
        "bedrock-agentcore:KmsKeyArn"
    ]
    assert pattern == "arn:aws:kms:*:*:key/*"


def test_scp_override_default_param():
    doc = load_control(
        "scp.memory.enforce-cmk",
        {"kms_key_arn_pattern": "arn:aws:kms:*:111122223333:key/*"},
    )
    pattern = doc["Statement"][0]["Condition"]["StringNotLike"][
        "bedrock-agentcore:KmsKeyArn"
    ]
    assert pattern == "arn:aws:kms:*:111122223333:key/*"


def test_resource_policy_substitutes_all_tokens():
    doc = load_control(
        "resource-policy.memory.in-account-only",
        {
            "account_id": "111122223333",
            "memory_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/mem-abc",
            "org_id": "o-example123",
        },
    )
    assert doc["Statement"][0]["Principal"]["AWS"] == "arn:aws:iam::111122223333:root"
    assert doc["Statement"][1]["Resource"].endswith("memory/mem-abc")
    assert (
        doc["Statement"][1]["Condition"]["StringNotEquals"]["aws:PrincipalOrgID"]
        == "o-example123"
    )
    # No sentinels should survive
    assert "<<" not in json.dumps(doc)


def test_missing_required_param_raises():
    with pytest.raises(ValueError, match="missing required params"):
        load_control("resource-policy.memory.in-account-only", {"account_id": "1"})


def test_unknown_control_id_raises():
    with pytest.raises(KeyError):
        load_control("does.not.exist")


def test_load_control_json_is_compact_string():
    s = load_control_json("scp.memory.enforce-cmk")
    assert isinstance(s, str)
    assert ", " not in s  # compact separators
    assert json.loads(s)["Version"] == "2012-10-17"


def test_guardrail_artifact_loads_with_expected_policies():
    doc = load_control("guardrail.egress-default")
    assert "BlockedInputMessaging" in doc
    filter_types = [f["Type"] for f in doc["ContentPolicyConfig"]["FiltersConfig"]]
    assert "PROMPT_ATTACK" in filter_types
    pii_types = {
        p["Type"] for p in doc["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    }
    assert {"EMAIL", "US_SOCIAL_SECURITY_NUMBER"} <= pii_types


def test_cedar_catalog_has_no_blanket_forbid():
    # Cedar is implicit default-deny; a blanket forbid would override every permit
    # (forbids always win in Cedar), turning ENFORCE into a full outage. Guard against
    # the control ever being reintroduced.
    catalog = load_catalog()
    ids = {c["id"] for c in catalog["controls"]}
    assert "cedar.gateway-default.forbid" not in ids
    permit = load_control_text("cedar.gateway-default.permit-read")
    assert "forbid(" not in permit


def test_cedar_permit_substitutes_read_action():
    text = load_control_text(
        "cedar.gateway-default.permit-read",
        {"read_action": "my-target___my_read_tool"},
    )
    assert 'AgentCore::Action::"my-target___my_read_tool"' in text
    assert "<<" not in text


# ── AgentCore Identity controls ──


def test_identity_userid_scp_denies_by_default():
    # GetWorkloadAccessTokenForUserId takes userId as an unverified string, so the shipped
    # default must exempt nobody. If someone gives the exemption param a real-looking
    # default, the control silently stops denying.
    doc = load_control("scp.identity.deny-workload-token-for-userid")
    statement = doc["Statement"][0]
    assert statement["Effect"] == "Deny"
    assert statement["Action"] == "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
    exempt = statement["Condition"]["ArnNotLike"]["aws:PrincipalArn"]
    assert "__no_principal_may_mint_tokens_by_userid__" in exempt


def test_identity_userid_scp_exemption_is_overridable():
    doc = load_control(
        "scp.identity.deny-workload-token-for-userid",
        {
            "approved_principal_arn_pattern": "arn:aws:iam::111122223333:role/break-glass"
        },
    )
    exempt = doc["Statement"][0]["Condition"]["ArnNotLike"]["aws:PrincipalArn"]
    assert exempt == "arn:aws:iam::111122223333:role/break-glass"


def test_identity_credential_provider_policy_names_one_provider():
    # AgentCore does not bind a workload identity to the providers it may read, so this
    # policy is the only thing stopping one agent reading another's stored credentials.
    # A wildcard in either resource defeats it entirely.
    doc = load_control(
        "iam.identity-credential-provider-scoped",
        {
            "region": "us-east-1",
            "account_id": "111122223333",
            "workload_identity_name": "finance-agent",
            "oauth2_provider_name": "entra-graph",
        },
    )
    by_sid = {s["Sid"]: s for s in doc["Statement"]}

    workload = by_sid["AllowOwnWorkloadIdentityTokens"]["Resource"]
    assert any(r.endswith("workload-identity/finance-agent") for r in workload)
    assert not any("*" in r for r in workload)

    provider = by_sid["AllowScopedOauth2CredentialProvider"]["Resource"]
    assert any(r.endswith("oauth2-credential-provider/entra-graph") for r in provider)
    assert not any("*" in r for r in provider)

    # Defence in depth: the role denies the unverified-userId path outright, so the SCP
    # is not the only thing standing between a compromised agent and other users' tokens.
    userid = by_sid["DenyWorkloadTokenForUserId"]
    assert userid["Effect"] == "Deny"
    assert userid["Action"] == "bedrock-agentcore:GetWorkloadAccessTokenForUserId"

    assert "<<" not in json.dumps(doc)


def test_identity_allow_statements_cover_every_required_resource_type():
    """GetResourceOauth2Token declares four REQUIRED resource types, not one.

    The Service Authorization Reference marks the directory, workload identity, token
    vault and credential provider all required (the * suffix) for these actions, so an
    Allow naming only the provider ARN authorises nothing. That failure is usually
    "fixed" by widening Resource to "*", which is the opposite of the intent — so assert
    the parent resources are present rather than trusting the leaf ARN alone.
    """
    doc = load_control(
        "iam.identity-credential-provider-scoped",
        {
            "region": "us-east-1",
            "account_id": "111122223333",
            "workload_identity_name": "finance-agent",
            "oauth2_provider_name": "entra-graph",
        },
    )
    by_sid = {s["Sid"]: s for s in doc["Statement"]}

    workload_required = (
        "workload-identity-directory/default",
        "workload-identity-directory/default/workload-identity/finance-agent",
    )
    resources = by_sid["AllowOwnWorkloadIdentityTokens"]["Resource"]
    for suffix in workload_required:
        assert any(r.endswith(suffix) for r in resources), (
            f"GetWorkloadAccessToken* needs a resource ending {suffix}; got {resources}"
        )

    oauth2_required = workload_required + (
        "token-vault/default",
        "token-vault/default/oauth2-credential-provider/entra-graph",
    )
    resources = by_sid["AllowScopedOauth2CredentialProvider"]["Resource"]
    for suffix in oauth2_required:
        assert any(r.endswith(suffix) for r in resources), (
            f"GetResourceOauth2Token needs a resource ending {suffix}; got {resources}"
        )


def test_identity_credential_provider_policy_requires_its_params():
    with pytest.raises(ValueError, match="missing required params"):
        load_control("iam.identity-credential-provider-scoped", {"region": "us-east-1"})


# ── Substitution semantics against a temp library ──


@pytest.fixture
def temp_library(tmp_path):
    (tmp_path / "vpce").mkdir()
    (tmp_path / "vpce" / "sample.json").write_text(
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Action": "bedrock-agentcore:InvokeGateway",
                        "Resource": "arn:aws:bedrock-agentcore:us-east-1:<<account_id>>:gateway/*",
                        "Condition": {
                            "StringNotEquals": {"aws:SourceVpce": "<<vpce_ids>>"}
                        },
                    }
                ],
            }
        )
    )
    (tmp_path / "catalog.yaml").write_text(
        "version: 1\n"
        "controls:\n"
        "  - id: vpce.sample\n"
        "    file: vpce/sample.json\n"
        "    type: VPCE\n"
        "    params:\n"
        "      account_id: {required: true}\n"
        "      vpce_ids: {required: true}\n"
    )
    return tmp_path


def test_list_typed_param_replaces_whole_node(temp_library):
    doc = load_control(
        "vpce.sample",
        {
            "account_id": "111122223333",
            "vpce_ids": ["vpce-aaa", "vpce-bbb"],
        },
        library_root=temp_library,
    )
    # Embedded token inside ARN string → substring replacement
    assert "111122223333" in doc["Statement"][0]["Resource"]
    # Whole-string token → replaced by the raw list value
    assert doc["Statement"][0]["Condition"]["StringNotEquals"]["aws:SourceVpce"] == [
        "vpce-aaa",
        "vpce-bbb",
    ]


def test_unresolved_token_raises(temp_library):
    # Only pass one of two required params by loosening the catalog would be needed;
    # instead prove the render guard by removing a default via direct file with stray token.
    (temp_library / "vpce" / "stray.json").write_text(
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Deny", "Action": "x", "Resource": "<<not_declared>>"}
                ],
            }
        )
    )
    (temp_library / "catalog.yaml").write_text(
        "version: 1\n"
        "controls:\n"
        "  - id: vpce.stray\n"
        "    file: vpce/stray.json\n"
        "    type: VPCE\n"
    )
    with pytest.raises(ValueError, match="unresolved tokens"):
        load_control("vpce.stray", library_root=temp_library)
