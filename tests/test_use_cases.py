"""The use-case extension point: manifests, opt-in, prerequisites, namespace.

Pure-Python (the CI pytest job has no aws_cdk). The synth half — that an
enabled use case's stack actually builds — is covered by check-contract.sh
via tests/fixtures/contract/usecase.yaml.
"""

import pytest

from infra_utils.platform_config import (
    PlatformConfig,
    UseCaseManifest,
    discover_use_cases,
)


def test_reference_use_case_is_discoverable():
    found = discover_use_cases()
    assert "hello-platform" in found
    m = found["hello-platform"]
    assert m.owner and m.summary  # contribution requirements, not decoration
    assert m.requires == ["gateway"]
    assert m.stacks == ["uc-hello-platform"]


def test_unknown_use_case_is_a_validation_error():
    with pytest.raises(ValueError, match="hello-platform"):
        # The error must LIST what is available, not just refuse.
        PlatformConfig.model_validate({"use_cases": {"no-such-thing": {}}})


def test_nothing_deploys_without_opt_in():
    config = PlatformConfig.model_validate({})
    assert not any("uc-" in s for s in config.expected_stacks())


def test_enabled_use_case_joins_the_footprint():
    config = PlatformConfig.model_validate({"use_cases": {"hello-platform": {}}})
    assert config.expected_stacks()[-1] == "agentcore-workshop-dev-uc-hello-platform"


def test_requires_is_checked_per_federation_role():
    # A federated WORKLOAD account has no local gateway: a gateway-requiring
    # use case must be refused there, with the reason in the message...
    config = PlatformConfig.model_validate(
        {
            "deployment": {
                "strategy": "federated",
                "platform_account": "111111111111",
                "workload_accounts": ["222222222222"],
            },
            "use_cases": {"hello-platform": {}},
        }
    )
    with pytest.raises(ValueError, match="gateway"):
        config.expected_stacks("222222222222")
    # ...and allowed on the platform side, which runs the gateway.
    assert any("uc-hello-platform" in s for s in config.expected_stacks("111111111111"))


def test_uc_namespace_is_enforced():
    with pytest.raises(ValueError, match="uc-"):
        UseCaseManifest.model_validate(
            {
                "name": "sneaky",
                "owner": "x",
                "summary": "tries to claim a core stack name",
                "stacks": ["gateway"],
            }
        )


def test_manifest_rejects_unknown_keys():
    with pytest.raises(ValueError):
        UseCaseManifest.model_validate(
            {
                "name": "typo-carrier",
                "owner": "x",
                "summary": "s",
                "stacks": ["uc-typo-carrier"],
                "stackz": ["oops"],
            }
        )
