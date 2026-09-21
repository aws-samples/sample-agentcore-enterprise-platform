"""The dashboard must agree with the CLI about what is deployed.

monitor.py owns status classification and scope; the browser renders what it
emits. These pin the three ways they used to disagree:

1. ROLLBACK_COMPLETE was counted as deployed AND failed (substring matches),
   which could make not_deployed negative.
2. The UI forced a denominator of at least 10, so a healthy greenfield
   deployment read 6/10 forever.
3. Module labels in monitor.py, index.html and deploy.sh all differed.
"""

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))

from monitor import (
    SAFE_EXPORT_STACK_OUTPUTS,
    SAFE_SSM_PARAMETERS,
    SAFE_STACK_OUTPUTS,
    STACK_META,
    PollError,
    classify,
    get_ssm_params,
    get_stack_status,
    poll,
    safe_stack_outputs,
    write_status,
)

INDEX_HTML = (REPO / "dashboard" / "public" / "index.html").read_text()
DEPLOY_SH = (REPO / "scripts" / "deploy.sh").read_text()


def test_rollback_is_failed_and_only_failed():
    # The bug: "COMPLETE" in ROLLBACK_COMPLETE counted it deployed too.
    assert classify("ROLLBACK_COMPLETE") == "failed"
    assert classify("UPDATE_ROLLBACK_COMPLETE") == "failed"
    assert classify("CREATE_FAILED") == "failed"


def test_the_other_states():
    assert classify("CREATE_COMPLETE") == "deployed"
    assert classify("UPDATE_COMPLETE") == "deployed"
    assert classify("CREATE_IN_PROGRESS") == "in-progress"
    assert classify("NOT_DEPLOYED") == "not-deployed"
    assert classify("DELETE_COMPLETE") == "not-deployed"
    assert classify("NOT_APPLICABLE") == "not-applicable"
    assert classify("") == "not-deployed"
    assert classify(None) == "not-deployed"


def test_states_are_mutually_exclusive():
    # Every state maps to exactly one bucket, so counting can never
    # double-count and the summary can never go negative.
    for status in (
        "CREATE_COMPLETE",
        "ROLLBACK_COMPLETE",
        "DELETE_COMPLETE",
        "CREATE_IN_PROGRESS",
        "NOT_DEPLOYED",
        "NOT_APPLICABLE",
    ):
        assert classify(status) in {
            "deployed",
            "in-progress",
            "failed",
            "not-deployed",
            "not-applicable",
        }


def test_ui_takes_the_denominator_from_the_contract():
    # The fake floor is gone...
    assert "Math.max(allNames.length, 10)" not in INDEX_HTML
    # ...and the count comes from monitor.py's summary.
    assert "summary.total_stacks" in INDEX_HTML


def test_ui_prefers_the_emitted_state():
    assert (
        "if (typeof stack === 'object' && stack?.state) return stack.state;"
        in INDEX_HTML
    )


GRAPH_JS = (REPO / "dashboard" / "public" / "graph.js").read_text()
GRAPH_CSS = (REPO / "dashboard" / "public" / "graph.css").read_text()


def test_xray_only_reads_real_stack_suffixes():
    body = GRAPH_JS.split("var STACK_SUFFIXES = [", 1)[1].split("];", 1)[0]
    suffixes = set(re.findall(r"'([^']+)'", body))
    assert suffixes == set(STACK_META)


def test_xray_reads_the_emitted_state():
    # Status classification remains exclusively owned by monitor.py.
    assert "record.state" in GRAPH_JS
    assert "function classify" not in GRAPH_JS


def test_xray_can_represent_unobserved_services_without_inventing_state():
    assert "unobserved" in GRAPH_JS
    assert "not observed" in GRAPH_JS.lower()


def test_xray_shape_checks_the_status_payload():
    assert "typeof status.stacks === 'object'" in GRAPH_JS
    assert "typeof status.deployment === 'object'" in GRAPH_JS


def test_architecture_tab_mounts_only_when_visible():
    tab_section = INDEX_HTML.split("function switchTab", 1)[1][:1600]
    assert "classList.add('active')" in tab_section
    assert tab_section.index("classList.add('active')") < tab_section.index(
        "ArchGraph.mount"
    )
    assert 'src="graph.js"' in INDEX_HTML
    assert 'href="graph.css"' in INDEX_HTML
    assert 'id="archFlow"' in INDEX_HTML


def test_xray_matches_the_agent_studio_interaction_model():
    assert "AgentCore X-Ray" in INDEX_HTML
    assert "Deployment map" not in INDEX_HTML
    assert "xr-band" in GRAPH_JS
    assert "xr-node-head" in GRAPH_JS
    assert "Reset layout" in GRAPH_JS
    assert "role', 'dialog'" in GRAPH_JS
    assert "Click a node to inspect it. Drag headers to rearrange." in GRAPH_JS


def test_xray_renders_accessible_dom_not_canvas():
    assert "getContext" not in GRAPH_JS
    assert "createElementNS" in GRAPH_JS
    assert "card.tabIndex = 0" in GRAPH_JS
    assert "textContent" in GRAPH_JS
    assert "innerHTML" not in GRAPH_JS


def test_xray_has_no_animation_loop():
    assert "requestAnimationFrame" not in GRAPH_JS
    assert "@keyframes" in GRAPH_CSS
    assert "prefers-reduced-motion" in GRAPH_CSS


def test_module_labels_match_deploy_sh():
    # MODULE_MAP[<id>]="<prefix>-<suffix> ..." — the CLI's own mapping.
    mapping: dict[str, set[str]] = {}
    for module, stacks in re.findall(r'MODULE_MAP\[(\w+)\]="([^"]*)"', DEPLOY_SH):
        for stack in stacks.split():
            mapping.setdefault(stack.replace("${PREFIX}-", ""), set()).add(module)
    for suffix, meta in STACK_META.items():
        if suffix in mapping:
            assert meta["module"] in mapping[suffix], (
                f"{suffix}: dashboard says module {meta['module']}, "
                f"deploy.sh says {sorted(mapping[suffix])}"
            )


def _client_error(code: str, message: str, operation: str = "DescribeStacks"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        operation,
    )


class _Paginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages


class _DescribedStack:
    def __init__(self, outputs):
        self.outputs = outputs
        self.resources = _Paginator([{"StackResourceSummaries": []}])

    def describe_stacks(self, **_kwargs):
        return {
            "Stacks": [
                {
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": self.outputs,
                }
            ]
        }

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        return self.resources


def test_cloudformation_outputs_are_allowlisted_before_serialization():
    generated_secret = "generated-client-secret-must-never-serialize"
    outputs = [
        {"OutputKey": "GatewayUrl", "OutputValue": "https://gateway.example"},
        {"OutputKey": "FutureOutput", "OutputValue": "not-reviewed"},
        {"OutputKey": "ClientSecret", "OutputValue": generated_secret},
        {
            "OutputKey": "ExportsOutputFnGetAttClientSecretABC",
            "OutputValue": generated_secret,
        },
    ]

    safe = safe_stack_outputs("gateway", outputs)

    assert safe == {"GatewayUrl": "https://gateway.example"}
    assert generated_secret not in json.dumps(safe)


def test_unknown_stack_type_has_no_public_outputs():
    assert (
        safe_stack_outputs(
            "future-stack",
            [{"OutputKey": "GatewayUrl", "OutputValue": "looks-safe"}],
        )
        == {}
    )


def test_export_allowlist_adds_only_federated_replacement_references():
    outputs = [
        {"OutputKey": "M2MClientIdV2Export", "OutputValue": "replacement-client"},
        {
            "OutputKey": "M2MClientSecretNameV2Export",
            "OutputValue": "replacement-secret-name",
        },
        {"OutputKey": "ClientSecret", "OutputValue": "credential-must-not-export"},
    ]

    assert safe_stack_outputs("auth", outputs) == {}
    assert safe_stack_outputs(
        "auth",
        outputs,
        allowlist=SAFE_EXPORT_STACK_OUTPUTS,
    ) == {
        "M2MClientIdV2Export": "replacement-client",
        "M2MClientSecretNameV2Export": "replacement-secret-name",
    }


def test_describe_stack_returns_only_reviewed_outputs():
    client = _DescribedStack(
        [
            {"OutputKey": "RuntimeArn", "OutputValue": "arn:aws:runtime"},
            {"OutputKey": "ClientSecret", "OutputValue": "never"},
        ]
    )

    result = get_stack_status(
        client,
        "project-dev-runtime-orchestrator",
        "runtime-orchestrator",
        "eu-west-1",
    )

    assert result["outputs"] == {"RuntimeArn": "arn:aws:runtime"}


def test_stack_resource_physical_ids_are_not_serialized():
    client = _DescribedStack([])
    client.resources = _Paginator(
        [
            {
                "StackResourceSummaries": [
                    {
                        "ResourceType": "Custom::External",
                        "LogicalResourceId": "External",
                        "ResourceStatus": "CREATE_COMPLETE",
                        "PhysicalResourceId": "opaque-provider-value",
                    }
                ]
            }
        ]
    )

    result = get_stack_status(client, "project-dev-gateway", "gateway", "us-east-1")

    assert result["resources"] == [
        {
            "type": "Custom::External",
            "logical": "External",
            "status": "CREATE_COMPLETE",
        }
    ]


def test_ssm_parameters_are_allowlisted_before_serialization():
    generated_secret = "ssm-secret-must-never-serialize"
    ssm = _SSM(
        [
            {
                "Parameters": [
                    {
                        "Name": "/project/dev/gateway/url",
                        "Value": "https://gateway.example",
                    },
                    {
                        "Name": "/project/dev/custom/display-name",
                        "Value": generated_secret,
                    },
                    {
                        "Name": "/project/dev/auth/m2m-client-secret-name",
                        "Value": "secret/reference/name",
                    },
                ]
            }
        ]
    )

    safe = get_ssm_params(ssm, "project", "dev", "eu-west-1")

    assert safe == {"gateway/url": "https://gateway.example"}
    assert generated_secret not in json.dumps(safe)


class _FailingCloudFormation:
    def __init__(self, error):
        self.error = error

    def describe_stacks(self, **_kwargs):
        raise self.error


def test_only_cloudformation_stack_not_found_maps_to_not_deployed():
    missing = _FailingCloudFormation(
        _client_error(
            "ValidationError",
            "Stack with id project-dev-auth does not exist",
        )
    )
    result = get_stack_status(
        missing,
        "project-dev-auth",
        "auth",
        "us-east-1",
    )
    assert result["status"] == "NOT_DEPLOYED"

    denied = _FailingCloudFormation(
        _client_error("AccessDenied", "not authorized: private detail")
    )
    with pytest.raises(PollError, match="denied") as denied_error:
        get_stack_status(
            denied,
            "project-dev-auth",
            "auth",
            "us-east-1",
        )
    assert denied_error.value.code == "AccessDenied"


def test_network_error_is_reported_instead_of_not_deployed():
    unavailable = _FailingCloudFormation(
        EndpointConnectionError(
            endpoint_url="https://cloudformation.eu-west-1.amazonaws.com"
        )
    )

    with pytest.raises(PollError) as error:
        get_stack_status(
            unavailable,
            "project-dev-auth",
            "auth",
            "eu-west-1",
        )

    assert error.value.code == "EndpointUnavailable"
    assert "eu-west-1" in error.value.message


class _STS:
    def get_caller_identity(self):
        return {"Account": "111111111111"}


class _MissingCloudFormation:
    def describe_stacks(self, **kwargs):
        raise _client_error(
            "ValidationError",
            f"Stack with id {kwargs['StackName']} does not exist",
        )


class _SSM:
    def __init__(self, pages=None):
        self.paginator = _Paginator(pages or [{"Parameters": []}])

    def get_paginator(self, name):
        assert name == "get_parameters_by_path"
        return self.paginator


def test_poll_resolves_config_and_environment_at_poll_time(tmp_path, monkeypatch):
    manifest = tmp_path / "platform.yaml"
    manifest.write_text("project: yaml-project\nenvironment: qa\nregion: eu-west-1\n")
    monkeypatch.setenv("PLATFORM_CONFIG", str(manifest))
    for name in (
        "PROJECT_NAME",
        "ENVIRONMENT",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(name, raising=False)

    ssm = _SSM()
    regions = []

    def factory(service, region_name):
        regions.append((service, region_name))
        return {
            "cloudformation": _MissingCloudFormation(),
            "ssm": ssm,
            "sts": _STS(),
        }[service]

    yaml_status = poll(factory)
    assert yaml_status["project"] == "yaml-project"
    assert yaml_status["environment"] == "qa"
    assert yaml_status["region"] == "eu-west-1"
    assert yaml_status["deployment_mode"] == "workshop"
    assert yaml_status["agent_pattern"] == "orchestrator"
    assert yaml_status["migration_runtime"] is False
    assert all(name.startswith("yaml-project-qa-") for name in yaml_status["stacks"])
    assert ssm.paginator.calls == [
        {
            "Path": "/yaml-project/qa",
            "Recursive": True,
            "WithDecryption": False,
        }
    ]

    monkeypatch.setenv("PROJECT_NAME", "env-project")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    env_status = poll(factory)
    assert env_status["project"] == "env-project"
    assert env_status["environment"] == "prod"
    assert env_status["region"] == "us-west-2"
    assert all(name.startswith("env-project-prod-") for name in env_status["stacks"])
    assert ("cloudformation", "us-west-2") in regions
    assert ("ssm", "us-west-2") in regions
    assert ("sts", "us-west-2") in regions


def test_poll_publishes_permission_error_without_stale_health(
    tmp_path,
    monkeypatch,
):
    manifest = tmp_path / "platform.yaml"
    manifest.write_text("project: safe-project\nenvironment: dev\nregion: us-east-1\n")
    monkeypatch.setenv("PLATFORM_CONFIG", str(manifest))
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    private_detail = "generated-client-secret-in-aws-error"
    denied = _FailingCloudFormation(
        _client_error("AccessDenied", f"not authorized: {private_detail}")
    )

    def factory(service, region_name):
        assert region_name == "us-east-1"
        return {
            "cloudformation": denied,
            "ssm": _SSM(),
            "sts": _STS(),
        }[service]

    status = poll(factory)
    serialized = json.dumps(status)

    assert status["available"] is False
    assert status["poll_error"]["code"] == "AccessDenied"
    assert "denied" in status["poll_error"]["message"]
    assert status["stacks"] == {}
    assert status["summary"]["deployed"] == 0
    assert private_detail not in serialized


def test_invalid_platform_config_fails_closed_without_polling_aws(
    tmp_path,
    monkeypatch,
):
    manifest = tmp_path / "platform.yaml"
    manifest.write_text("agents:\n  pattern: definitely-not-supported\n")
    monkeypatch.setenv("PLATFORM_CONFIG", str(manifest))
    calls = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid configuration must not poll AWS")

    status = poll(factory)

    assert status["available"] is False
    assert status["poll_error"]["service"] == "Configuration"
    assert status["poll_error"]["code"] == "InvalidConfiguration"
    assert status["stacks"] == {}
    assert status["summary"]["deployed"] == 0
    assert calls == []


def test_status_write_is_atomic_and_owner_only(tmp_path):
    destination = tmp_path / "status.json"
    destination.write_text('{"old": true}\n')

    write_status(destination, {"available": True, "value": 1})

    assert json.loads(destination.read_text()) == {
        "available": True,
        "value": 1,
    }
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".status.json.*"))


def test_written_dashboard_artifact_excludes_unreviewed_secret_output(tmp_path):
    generated_secret = "AKIA" + "1234567890ABCDEF"
    outputs = [
        {"OutputKey": "GatewayUrl", "OutputValue": "https://gateway.example"},
        {"OutputKey": "ClientSecret", "OutputValue": generated_secret},
    ]
    destination = tmp_path / "status.json"
    status = {
        "available": True,
        "stacks": {
            "project-dev-gateway": {"outputs": safe_stack_outputs("gateway", outputs)}
        },
    }

    write_status(destination, status)

    serialized = destination.read_text()
    written = json.loads(serialized)
    assert written["stacks"]["project-dev-gateway"]["outputs"] == {
        "GatewayUrl": "https://gateway.example"
    }
    assert generated_secret not in serialized


def test_failed_status_write_keeps_previous_complete_file(tmp_path):
    destination = tmp_path / "status.json"
    destination.write_text('{"old": true}\n')

    with pytest.raises(TypeError):
        write_status(destination, {"bad": object()})

    assert json.loads(destination.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".status.json.*"))


def test_ui_never_offers_secret_reveal_or_copy():
    for forbidden in (
        "data-reveal",
        "data-full",
        "data-copy",
        "navigator.clipboard",
        "function copyBtn",
        "SECRET_RE",
    ):
        assert forbidden not in INDEX_HTML


def test_ui_sanitizes_payload_before_any_rendering():
    fetch = INDEX_HTML.split("async function fetchStatus()", 1)[1]
    sanitize_at = fetch.index("sanitizeStatus(await res.json())")
    store_at = fetch.index("lastStatus = data")
    render_at = fetch.index("render(data)")
    assert sanitize_at < store_at < render_at
    assert "SAFE_OUTPUT_KEYS.has(key)" in INDEX_HTML
    assert "SAFE_PARAMETER_KEYS.has(key)" in INDEX_HTML


def test_ui_hides_health_when_poll_fails_or_becomes_stale():
    unavailable = INDEX_HTML.split("function showUnavailable", 1)[1].split(
        "/* ── OVERVIEW", 1
    )[0]
    assert "$('stateOnline').hidden = true" in unavailable
    assert "el.hidden = true" in unavailable
    stale = INDEX_HTML.split("function tickLive()", 1)[1].split(
        "function startPolling", 1
    )[0]
    assert "showUnavailable(" in stale
    assert "!Number.isFinite(age) || age < -5" in stale
    assert "Invalid status timestamp" in stale


def test_documented_dashboard_servers_are_loopback_only():
    for relative in (
        "README.md",
        "docs/PARTICIPANT_GUIDE.md",
    ):
        text = (REPO / relative).read_text()
        commands = [
            line for line in text.splitlines() if "python3 -m http.server" in line
        ]
        assert commands, f"{relative} has no dashboard server command"
        assert all("--bind 127.0.0.1" in line for line in commands), relative


def test_browser_allowlist_covers_backend_reviewed_outputs():
    browser_list = INDEX_HTML.split(
        "const SAFE_OUTPUT_KEYS = new Set([",
        1,
    )[1].split("]);", 1)[0]
    browser_keys = set(re.findall(r"'([^']+)'", browser_list))
    backend_keys = set().union(*SAFE_STACK_OUTPUTS.values())
    assert backend_keys <= browser_keys
    assert not any(
        re.search(r"secret|password|token|api-?key", key, re.IGNORECASE)
        for key in browser_keys
    )

    browser_parameters = INDEX_HTML.split(
        "const SAFE_PARAMETER_KEYS = new Set([",
        1,
    )[1].split("]);", 1)[0]
    browser_parameter_keys = set(re.findall(r"'([^']+)'", browser_parameters))
    assert browser_parameter_keys == set(SAFE_SSM_PARAMETERS)
