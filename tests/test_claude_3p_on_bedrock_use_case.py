"""claude-3p-on-bedrock: config schema, team -> policy mapping, gateway.yaml
rendering, IAM scoping, the in-VPC probe, and the verify contract.

Pure Python (the CI pytest job has no aws_cdk). That the stack synthesizes and
lands in the deployment contract is covered by scripts/check-contract.sh via
tests/fixtures/contract/claude-3p-on-bedrock.yaml.
"""

import copy
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
UC = REPO / "use-cases" / "claude-3p-on-bedrock"
FIXTURE = REPO / "tests" / "fixtures" / "contract" / "claude-3p-on-bedrock.yaml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gc = _load("c3p_gateway_config", UC / "gateway" / "config.py")
probe = None  # loaded lazily: the handler reads PUBLIC_URL at import


@pytest.fixture(autouse=True)
def _allow_fixture_placeholders(monkeypatch):
    # The fixture uses example values on purpose, like CI (check-contract.sh).
    monkeypatch.setenv("PLATFORM_ALLOW_PLACEHOLDERS", "1")


@pytest.fixture
def raw() -> dict:
    """The use-case block from the contract fixture, a fresh copy per test."""
    doc = yaml.safe_load(FIXTURE.read_text())
    return copy.deepcopy(doc["use_cases"]["claude-3p-on-bedrock"])


def rendered(raw: dict, region: str = "us-east-1") -> dict:
    text = gc.render_gateway_yaml(gc.load(raw), region)
    return yaml.safe_load(text)


# ── schema ──────────────────────────────────────────────────────────────────


def test_fixture_is_valid(raw):
    cfg = gc.load(raw)
    assert cfg.public_url == "https://claude.internal.example.com"
    assert cfg.oidc.issuer.endswith("/v2.0")


def test_unknown_keys_are_errors(raw):
    raw["modles"] = []  # a typo must fail, not be ignored
    with pytest.raises(ValueError, match="modles"):
        gc.load(raw)


@pytest.mark.parametrize(
    "missing",
    ["claude_version", "hostname", "certificate_arn", "oidc", "models", "default"],
)
def test_customer_values_are_required(raw, missing):
    del raw[missing]
    with pytest.raises(ValueError, match=missing):
        gc.load(raw)


def test_dns_defaults_to_customer_managed(raw):
    assert gc.load(raw).dns.mode == "customer"


def test_route53_mode_needs_a_zone(raw):
    raw["dns"] = {"mode": "route53"}
    with pytest.raises(ValueError, match="needs dns.private_zone"):
        gc.load(raw)


def test_customer_mode_rejects_a_zone(raw):
    raw["dns"] = {"mode": "customer", "private_zone": {"name": "internal.example.com"}}
    with pytest.raises(ValueError, match="only used with dns.mode: route53"):
        gc.load(raw)


def test_route53_hostname_must_be_in_zone(raw):
    raw["dns"] = {"mode": "route53", "private_zone": {"name": "other.example.org"}}
    with pytest.raises(ValueError, match="not inside dns.private_zone"):
        gc.load(raw)


def test_team_model_must_exist_in_catalog(raw):
    raw["teams"][0]["models"] = ["claude-nonexistent"]
    with pytest.raises(ValueError, match="not in models"):
        gc.load(raw)


def test_default_model_must_exist_in_catalog(raw):
    raw["default"]["models"] = ["claude-nonexistent"]
    with pytest.raises(ValueError, match=r"default\.models"):
        gc.load(raw)


def test_shadowed_team_is_an_error_in_first_match(raw):
    raw["teams"].append(
        {
            "name": "dup",
            "entra_roles": ["Claude.Contractors"],
            "models": ["claude-haiku-4-5"],
        }
    )
    with pytest.raises(ValueError, match="unreachable"):
        gc.load(raw)


def test_shadowed_team_is_allowed_when_layered(raw):
    raw["teams"].append(
        {
            "name": "dup",
            "entra_roles": ["Claude.Contractors"],
            "models": ["claude-haiku-4-5"],
        }
    )
    raw["policy_mode"] = "layered"
    raw["claude_version"] = "2.1.316"
    gc.load(raw)


def test_empty_role_is_rejected(raw):
    raw["teams"][0]["entra_roles"] = [" "]
    with pytest.raises(ValueError, match="non-empty"):
        gc.load(raw)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"claude_version": "2.1.200"}, "claude_version must be >= 2.1.274"),
        ({"policy_mode": "layered", "claude_version": "2.1.311"}, "layered needs"),
        (
            {"guardrail": {"id": "gr1", "version": "1"}, "claude_version": "2.1.276"},
            "guardrail needs",
        ),
        (
            {
                "guardrail": {
                    "id": "gr1",
                    "version": "1",
                    "input_tagging": "user_messages",
                },
                "claude_version": "2.1.311",
            },
            "input_tagging needs",
        ),
    ],
)
def test_version_gates(raw, change, message):
    # An older gateway refuses to start on these keys; fail at design instead.
    raw.update(change)
    with pytest.raises(ValueError, match=message):
        gc.load(raw)


def test_ipv6_client_cidr_rejected(raw):
    raw["client_cidrs"] = ["fd00::/64"]
    with pytest.raises(ValueError, match="IPv4 only"):
        gc.load(raw)


def test_client_cidrs_must_not_overlap_trusted_proxies(raw):
    raw["client_cidrs"] = ["10.0.2.0/25"]
    with pytest.raises(ValueError, match="overlaps trusted_proxies"):
        gc.load(raw)


def test_collector_latest_tag_rejected(raw):
    raw["telemetry"] = {
        "mode": "sidecar",
        "collector_image": "public.ecr.aws/x/y:latest",
    }
    with pytest.raises(ValueError, match="latest"):
        gc.load(raw)


def test_settings_cannot_shadow_models(raw):
    raw["default"]["settings"] = {"availableModels": ["claude-opus-5"]}
    with pytest.raises(ValueError, match="through `models`"):
        gc.load(raw)


def test_guardrail_version_must_be_string_shaped(raw):
    raw["guardrail"] = {"id": "gr1", "version": "latest"}
    with pytest.raises(ValueError, match="version"):
        gc.load(raw)


# ── rendering ────────────────────────────────────────────────────────────────


def test_rendered_file_holds_no_secret_values(raw):
    text = gc.render_gateway_yaml(gc.load(raw), "us-east-1")
    doc = yaml.safe_load(text)
    assert doc["oidc"]["client_secret"] == "${OIDC_CLIENT_SECRET}"
    assert doc["session"]["jwt_secret"] == "${GATEWAY_JWT_SECRET}"
    assert doc["store"]["password"] == "${DB_PASSWORD}"
    assert "sslmode=verify-full" in doc["store"]["postgres_url"]


def test_entra_oidc_block(raw):
    oidc = rendered(raw)["oidc"]
    assert (
        oidc["issuer"]
        == "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0"
    )
    assert oidc["groups_claim"] == "roles"  # App Roles: readable names, not GUIDs
    assert "allowed_groups" not in oidc


def test_only_catalog_models_are_served(raw):
    doc = rendered(raw)
    assert doc["auto_include_builtin_models"] is False
    assert [m["id"] for m in doc["models"]] == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]


def test_teams_become_policies_in_order_with_base_last(raw):
    pols = rendered(raw)["managed"]["policies"]
    assert [p["match"] for p in pols] == [
        {"groups": ["Claude.Contractors"]},
        {"groups": ["Claude.Engineering"]},
        {},
    ]
    contractors, engineering, base = pols
    assert contractors["cli"]["availableModels"] == ["claude-haiku-4-5"]
    assert contractors["cli"]["permissions"]["deny"] == ["WebFetch", "WebSearch"]
    assert "enforceAvailableModels" not in engineering["cli"]  # inherited from base
    assert base["cli"]["enforceAvailableModels"] is True
    assert base["desktop"] == {}


def test_cli_key_silences_desktop_warning_on_every_policy(raw):
    pols = rendered(raw)["managed"]["policies"]
    assert all(p["serve_to_desktop"] is False for p in pols)


def test_deny_tools_merge_with_settings_permissions(raw):
    raw["default"]["settings"] = {
        "permissions": {"deny": ["Bash(rm:*)"], "ask": ["Bash"]}
    }
    base = rendered(raw)["managed"]["policies"][-1]["cli"]
    assert base["permissions"]["deny"] == [
        "Bash(rm:*)",
        "Read(./.env)",
        "Read(./secrets/**)",
    ]
    assert base["permissions"]["ask"] == ["Bash"]


def test_layered_mode_is_emitted(raw):
    raw.update(policy_mode="layered", claude_version="2.1.316")
    assert rendered(raw)["managed"]["policy_mode"] == "layered"


def test_spend_admin_roles_enable_admin_api(raw):
    assert rendered(raw)["admin"] == {"admin_groups": ["Claude.SpendAdmins"]}


def test_ip_allowlist_is_opt_in(raw):
    doc = rendered(raw)
    assert "access_control" not in doc  # trusted_proxies alone refuses no one
    assert doc["listen"]["trusted_proxies"] == ["10.0.2.0/24", "10.0.3.0/24"]
    raw["gateway_ip_allowlist"] = True
    assert rendered(raw)["access_control"] == {"allow_cidrs": ["172.16.0.0/22"]}


def test_ip_allowlist_needs_trusted_proxies(raw):
    raw.update(gateway_ip_allowlist=True, trusted_proxies=[])
    with pytest.raises(ValueError, match="needs trusted_proxies"):
        gc.load(raw)


def test_guardrail_rendered_with_string_version(raw):
    raw["guardrail"] = {"id": "gr-abc", "version": "3"}
    up = rendered(raw)["upstreams"][0]
    assert up["guardrail"] == {"id": "gr-abc", "version": "3"}
    assert gc.render_gateway_yaml(gc.load(raw), "us-east-1").count("version: '3'") == 1


def test_bedrock_region_override(raw):
    raw["bedrock_region"] = "eu-central-1"
    assert rendered(raw)["upstreams"][0]["region"] == "eu-central-1"


def test_telemetry_sidecar_forwards_metrics_only(raw):
    raw["telemetry"] = {"mode": "sidecar"}
    assert rendered(raw)["telemetry"] == {
        "forward_to": [{"url": "http://127.0.0.1:4318", "metrics": True}]
    }


# ── IAM scoping ──────────────────────────────────────────────────────────────


def test_bedrock_arns_cover_profile_and_foundation_model(raw):
    raw["models"].append(
        {
            "id": "pt",
            "label": "PT",
            "bedrock": "arn:aws:bedrock:us-east-1:1:provisioned-model/x",
        }
    )
    raw["models"].append(
        {"id": "geo", "label": "Geo", "bedrock": "eu.anthropic.claude-sonnet-5"}
    )
    arns = gc.bedrock_resource_arns(gc.load(raw), "us-east-1", "111111111111")
    assert (
        "arn:aws:bedrock:us-east-1:111111111111:inference-profile/global.anthropic.claude-opus-5"
        in arns
    )
    assert "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5" in arns
    assert (
        "arn:aws:bedrock:us-east-1:111111111111:inference-profile/eu.anthropic.claude-sonnet-5"
        in arns
    )
    assert "arn:aws:bedrock:us-east-1:1:provisioned-model/x" in arns
    assert not any(
        a.endswith("anthropic.*") for a in arns
    )  # no wildcard over the catalog


def test_foundation_model_id():
    assert (
        gc.foundation_model_id("global.anthropic.claude-opus-5")
        == "anthropic.claude-opus-5"
    )
    assert (
        gc.foundation_model_id("anthropic.claude-opus-5") == "anthropic.claude-opus-5"
    )
    assert gc.foundation_model_id("arn:aws:bedrock:us-east-1:1:x") is None


# ── probe ────────────────────────────────────────────────────────────────────


@pytest.fixture
def probe_mod(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://claude.internal.example.com")
    return _load("c3p_probe", UC / "gateway" / "probe" / "handler.py")


def test_probe_ok_when_all_checks_pass(probe_mod, monkeypatch):
    responses = {
        "/healthz": (200, "ok"),
        "/readyz": (200, "ok"),
        "/.well-known/oauth-authorization-server": (
            200,
            '{"issuer": "https://claude.internal.example.com"}',
        ),
    }
    monkeypatch.setattr(probe_mod, "_get", lambda path: responses[path])
    out = probe_mod.handler({}, None)
    assert out["ok"] is True


def test_probe_fails_on_issuer_mismatch(probe_mod, monkeypatch):
    responses = {
        "/healthz": (200, "ok"),
        "/readyz": (200, "ok"),
        "/.well-known/oauth-authorization-server": (
            200,
            '{"issuer": "https://elsewhere.example.com"}',
        ),
    }
    monkeypatch.setattr(probe_mod, "_get", lambda path: responses[path])
    assert probe_mod.handler({}, None)["ok"] is False


def test_probe_fails_and_reports_without_raising(probe_mod, monkeypatch):
    def boom(path):
        raise OSError("name resolution failed")

    monkeypatch.setattr(probe_mod, "_get", boom)
    out = probe_mod.handler({}, None)
    assert out["ok"] is False
    assert "name resolution failed" in out["checks"]["healthz"]["error"]


# ── verify contract ──────────────────────────────────────────────────────────


def test_verify_fails_fast_without_a_deployment():
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),  # nosec B108 - test scaffolding
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": "us-east-1",
    }
    r = subprocess.run(  # nosec B603
        [sys.executable, str(UC / "verify.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r.returncode == 1
    assert r.stderr.startswith("FAIL:"), r.stderr
    assert "Traceback" not in r.stderr


# ── review fixes ─────────────────────────────────────────────────────────────


def test_every_policy_starts_on_an_allowed_model(raw):
    # Without `model`, sessions start on the built-in Opus and get 400s.
    pols = rendered(raw)["managed"]["policies"]
    assert [p["cli"]["model"] for p in pols] == [
        "claude-haiku-4-5",
        "claude-opus-5",
        "claude-haiku-4-5",
    ]


def test_start_model_override_must_be_listed(raw):
    raw["teams"][1]["start_model"] = "claude-sonnet-5"
    assert rendered(raw)["managed"]["policies"][1]["cli"]["model"] == "claude-sonnet-5"
    raw["teams"][0]["start_model"] = "claude-opus-5"  # contractors may not use Opus
    with pytest.raises(ValueError, match="start_model"):
        gc.load(raw)


def test_model_cannot_be_set_through_settings(raw):
    raw["default"]["settings"] = {"model": "claude-opus-5"}
    with pytest.raises(ValueError, match="start_model"):
        gc.load(raw)


def test_empty_model_list_emits_no_start_model(raw):
    raw["default"]["models"] = []
    base = rendered(raw)["managed"]["policies"][-1]["cli"]
    assert base["availableModels"] == []
    assert "model" not in base


def test_inference_profile_arn_needs_and_grants_foundation_model(raw):
    arn = "arn:aws:bedrock:us-east-1:111111111111:application-inference-profile/abc123"
    raw["models"].append({"id": "team-opus", "label": "Team Opus", "bedrock": arn})
    with pytest.raises(ValueError, match="foundation_model"):
        gc.load(raw)
    raw["models"][-1]["foundation_model"] = "anthropic.claude-opus-5"
    arns = gc.bedrock_resource_arns(gc.load(raw), "us-east-1", "111111111111")
    assert arn in arns
    assert "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5" in arns


def test_drain_seconds_bounds(raw):
    raw["service"] = {"drain_seconds": 10}
    with pytest.raises(ValueError, match="drain_seconds"):
        gc.load(raw)


def test_probe_allowlist_mode_expects_refusal(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://claude.internal.example.com")
    monkeypatch.setenv("EXPECT_METADATA_STATUS", "403")
    mod = _load("c3p_probe_allowlist", UC / "gateway" / "probe" / "handler.py")
    responses = {
        "/healthz": (200, "ok"),
        "/readyz": (200, "ok"),
        "/.well-known/oauth-authorization-server": (403, "forbidden"),
    }
    monkeypatch.setattr(mod, "_get", lambda path: responses[path])
    out = mod.handler({}, None)
    assert out["ok"] is True
    assert out["allowlist_mode"] is True
    # Allow-list configured but not enforced: must FAIL.
    responses["/.well-known/oauth-authorization-server"] = (
        200,
        '{"issuer": "https://claude.internal.example.com"}',
    )
    assert mod.handler({}, None)["ok"] is False


def test_probe_connects_to_alb_name_with_gateway_hostname(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://claude.corp.example.com")
    monkeypatch.setenv("CONNECT_HOST", "internal-x.us-east-1.elb.amazonaws.com")
    mod = _load("c3p_probe_connect", UC / "gateway" / "probe" / "handler.py")
    conn = mod._PinnedConnection(mod.CONNECT_HOST, mod.HOSTNAME, mod._context())
    assert conn._connect_host == "internal-x.us-east-1.elb.amazonaws.com"
    assert (
        conn.host == "claude.corp.example.com"
    )  # SNI, certificate check and Host header


@pytest.mark.parametrize(
    "bad", ["claude-3p/entra-client-secret", "app-XEokdd", "x-abcdef"]
)
def test_secret_name_must_not_look_like_an_arn_suffix(raw, bad):
    # Found live: ECS uses the partial ARN; Secrets Manager reads "-secret" as the suffix.
    raw["oidc"]["client_secret_name"] = bad
    with pytest.raises(ValueError, match="ARN suffix"):
        gc.load(raw)


@pytest.mark.parametrize(
    "good", ["claude-3p/entra-oidc", "claude-3p/entra", "x-abcde", "x-abcdefg"]
)
def test_secret_names_that_are_safe(raw, good):
    raw["oidc"]["client_secret_name"] = good
    gc.load(raw)


def test_entra_role_with_spaces_is_rejected(raw):
    # Found in the wizard test: pasted text ended up as a role value and passed every check.
    raw["teams"][1]["entra_roles"] = ["=== 6. Teams (Entra App Role -> models) ==="]
    with pytest.raises(ValueError, match="has no spaces"):
        gc.load(raw)


# ── example platform.yaml ────────────────────────────────────────────────────

EXAMPLE = UC / "docs" / "platform.example.yaml"


def test_example_platform_yaml_is_valid_once_values_are_filled(monkeypatch):
    from infra_utils.platform_config import load_platform_config

    load_platform_config(EXAMPLE)  # placeholders allowed (autouse): shape is valid
    gc.load(yaml.safe_load(EXAMPLE.read_text())["use_cases"]["claude-3p-on-bedrock"])


def test_example_platform_yaml_is_refused_until_edited(monkeypatch):
    monkeypatch.delenv("PLATFORM_ALLOW_PLACEHOLDERS")
    block = yaml.safe_load(EXAMPLE.read_text())["use_cases"]["claude-3p-on-bedrock"]
    with pytest.raises(ValueError) as exc:
        gc.load(block)
    msg = str(exc.value)
    for field in (
        "oidc.tenant_id",
        "oidc.client_id",
        "hostname",
        "certificate_arn",
        "allowed_email_domains",
    ):
        assert field in msg, field
    # Real-looking values pass.
    block.update(
        hostname="claude.contoso.net",
        certificate_arn=block["certificate_arn"]
        .replace("000000000000", "123412341234")
        .replace(
            "00000000-0000-0000-0000-000000000000",
            "1a2b3c4d-1111-2222-3333-444455556666",
        ),
    )
    block["oidc"].update(
        tenant_id="9a8b7c6d-1111-2222-3333-444455556666",
        client_id="1f2e3d4c-1111-2222-3333-444455556666",
        allowed_email_domains=["contoso.net"],
    )
    gc.load(block)
