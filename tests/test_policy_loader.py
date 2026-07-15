"""Unit tests for infra_utils.policy_loader."""

import json

import pytest

from infra_utils.policy_loader import load_catalog, load_control, load_control_json


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
