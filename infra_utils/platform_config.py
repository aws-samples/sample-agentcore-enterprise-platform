"""platform.yaml — the declarative deployment config for the accelerator.

One file describes a deployment: project identity, multi-account strategy,
IdP, agent pattern, gateway tools, security controls, observability. The five
workshop profiles ship as presets/*.yaml built from these same models, so
presets double as validated fixtures.

Validation is error-ACCUMULATING (pydantic collects every failure in one
pass): a participant with three typos gets three messages, not three deploy
cycles. Validate without touching AWS:

    python -m infra_utils.platform_config platform.yaml

Precedence when consumed (matches the pre-existing context-over-env order):
    app.py:    cdk context  >  env var  >  platform.yaml  >  legacy defaults
    deploy.sh: explicit env >  platform.yaml  >  workshop.env  >  prompts
platform.yaml is optional — every existing flag keeps working without it.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

# Where the Web Search built-in gateway connector exists (launch regions).
WEB_SEARCH_REGIONS = {"us-east-1", "eu-west-1", "ap-northeast-1"}

# ── Region tables (static; see docs/SUPPORT_MATRIX.md for how to update) ──
# Base model ids offered as cross-region inference profiles per geo prefix.
# These drift from AWS: findings derived from them are warnings only; `doctor`
# and `verify` remain the authoritative live checks.
INFERENCE_PROFILES_BY_GEO: dict[str, frozenset[str]] = {
    "us": frozenset(
        {
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "anthropic.claude-3-7-sonnet-20250219-v1:0",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            "anthropic.claude-opus-4-1-20250805-v1:0",
            "amazon.nova-pro-v1:0",
            "amazon.nova-lite-v1:0",
            "amazon.nova-micro-v1:0",
        }
    ),
    "eu": frozenset(
        {
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "anthropic.claude-3-7-sonnet-20250219-v1:0",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "amazon.nova-pro-v1:0",
            "amazon.nova-lite-v1:0",
            "amazon.nova-micro-v1:0",
        }
    ),
    "apac": frozenset(
        {
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "anthropic.claude-3-7-sonnet-20250219-v1:0",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "amazon.nova-pro-v1:0",
            "amazon.nova-lite-v1:0",
            "amazon.nova-micro-v1:0",
        }
    ),
    "global": frozenset(
        {
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        }
    ),
}

# Region -> the geo prefix of the cross-region inference profiles it serves.
REGION_GEO: dict[str, str] = {
    "us-east-1": "us",
    "us-east-2": "us",
    "us-west-1": "us",
    "us-west-2": "us",
    "eu-central-1": "eu",
    "eu-central-2": "eu",
    "eu-north-1": "eu",
    "eu-south-1": "eu",
    "eu-south-2": "eu",
    "eu-west-1": "eu",
    "eu-west-2": "eu",
    "eu-west-3": "eu",
    "ap-northeast-1": "apac",
    "ap-northeast-2": "apac",
    "ap-northeast-3": "apac",
    "ap-south-1": "apac",
    "ap-south-2": "apac",
    "ap-southeast-1": "apac",
    "ap-southeast-2": "apac",
}

_ACCOUNT_RE = r"^\d{12}$"
_REGION_RE = r"^[a-z]{2}(-[a-z]+)+-\d$"

# ── Placeholders ──
# A sentinel that validates deploys green and fails at the first sign-in, an
# hour later, with an IdP error that says nothing about the config. Every
# check below names the field and says what belongs there. The parity gate
# (scripts/check-contract.sh) synthesizes the shipped presets WITH their
# placeholders in place, so it exports PLATFORM_ALLOW_PLACEHOLDERS=1: the same
# messages then surface as PlatformConfig.warnings instead of errors. Nothing
# in deploy.sh sets it — a real deploy always fails closed.
_SENTINEL_RE = re.compile(
    r"replace|change.?me|\btodo\b|^<.*>$|example\.com", re.IGNORECASE
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)
# Secrets Manager NAMES: alphanumerics and /_+=.@- only. A pasted secret VALUE
# fails this (whitespace, '~'), or the JWT/base64-blob shapes below.
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,200}$")
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.")
# ponytail: a 32+ char run with no separator is called a value, not a name —
# a real name that long without any of /_.-@ is refused too. Upgrade path:
# an allow-list of known-good names, if one ever exists.
_BLOB_RE = re.compile(r"^[A-Za-z0-9+=]{32,}$")
# 111111111111 is deliberately NOT here: it is the parity gate's fake account
# (check-contract.sh) and a real customer cannot be allocated a
# repeating-digit id, so accepting it costs nothing.
_ACCOUNT_SENTINELS = {"000000000000", "123456789012"}


def _single_line_reference(value: str, label: str = "reference") -> str:
    """Reject terminal controls and ambiguous surrounding whitespace."""
    if value != value.strip() or (value and not value.isprintable()):
        raise ValueError(f"must be a trimmed, printable single-line {label}")
    return value


def placeholders_allowed() -> bool:
    return os.environ.get("PLATFORM_ALLOW_PLACEHOLDERS") == "1"


def _refuse_placeholders(messages: list[str]) -> None:
    if messages and not placeholders_allowed():
        raise ValueError("\n".join(messages))


DEPLOYMENT_MODES = ("workshop", "production")


def resolve_deployment_mode(
    manifest_mode: str | None,
    context_value: str = "",
    env_value: str = "",
    allow_override: bool = False,
) -> str:
    """The deployment mode app.py synthesizes, with platform.yaml as the truth.

    With a manifest, ``deployment.mode`` wins. A ``-c deployment_mode`` or
    ``DEPLOYMENT_MODE`` value that differs from it is refused, so a stray
    environment variable in a CI shell cannot silently downgrade production.
    Only the explicit ``-c allow_mode_override=true`` accepts the requested
    value (context before env), with a loud warning on stderr. Without a
    manifest the requested value (default workshop) is used as before.
    """
    requested = [
        (source, value)
        for source, value in (
            ("-c deployment_mode", context_value),
            ("DEPLOYMENT_MODE", env_value),
        )
        if value
    ]
    for source, value in requested:
        if value not in DEPLOYMENT_MODES:
            raise ValueError(
                f"deployment_mode must be 'workshop' or 'production', got "
                f"{value!r} from {source}"
            )
    if manifest_mode is None:
        return requested[0][1] if requested else "workshop"
    conflicts = [(s, v) for s, v in requested if v != manifest_mode]
    if not conflicts:
        return manifest_mode
    described = ", ".join(f"{s}={v}" for s, v in conflicts)
    if not allow_override:
        raise ValueError(
            f"platform.yaml declares deployment.mode: {manifest_mode}, but "
            f"{described} asks for a different mode. platform.yaml is the source "
            "of truth: unset the variable/context, or change deployment.mode in "
            "the file. To force it anyway pass -c allow_mode_override=true."
        )
    mode = requested[0][1]
    print(
        "\n" + "!" * 72 + "\n"
        f"[WARN] DEPLOYMENT MODE OVERRIDDEN: platform.yaml says {manifest_mode!r} "
        f"but {described} is in effect; synthesizing in {mode!r} mode because "
        "allow_mode_override=true.\n" + "!" * 72,
        file=sys.stderr,
    )
    return mode


def resolve_synth_region(
    context_region: str = "", manifest_region: str = "", env_region: str = ""
) -> str:
    """The region app.py synthesizes into.

    ``-c region`` wins, then the manifest's ``region``. ``CDK_DEFAULT_REGION``
    (set by the CDK CLI from the active profile) only fills a missing value,
    so a plain ``cdk synth`` cannot silently build in the wrong region.
    """
    return context_region or manifest_region or env_region or "us-east-1"


# ── Layered presets: journey x topology x posture ──
# The eight flat presets bundle three independent choices. These layers are
# those choices factored apart; presets/*.yaml stay as golden references and
# tests/test_preset_layers.py proves every shortcut composes to the same
# design. Merge rules: layers apply journey -> topology -> posture; mappings
# merge recursively, scalars and lists are replaced by the later layer, so
# posture wins for security:. The agents: subtree is owned by the journey:
# a later layer may only fill agents keys the journey leaves unset, never
# change one. Shortcut overlays apply last and may set any key.
_ENTRA_PLACEHOLDER_IDENTITY = {
    "idp": "entra_id",
    "tenant_id": "00000000-0000-0000-0000-000000000000",
    "client_id": "REPLACE_ME",
    "client_secret_name": "agentcore/idp-client-secret",  # pragma: allowlist secret
}


def _journey(agents: dict, **extra: object) -> dict:
    layer: dict = {
        "project": "agentcore-workshop",
        "environment": "dev",
        "region": "us-east-1",
        "identity": {"idp": "cognito"},
        "agents": agents,
        "gateway": {"web_search": "auto", "tools": ["sample-tool"]},
        "security": {},
        "observability": {"transaction_search": True},
    }
    layer.update(extra)
    return layer


JOURNEYS: dict[str, dict] = {
    "greenfield": _journey({"pattern": "orchestrator"}),
    "migration": _journey(
        {"pattern": "langgraph-agent"},
        deployment={"platform_account": "000000000000"},
        identity=dict(_ENTRA_PLACEHOLDER_IDENTITY),
        migration={
            "source": {
                "platform": "openshift",
                "build": {
                    "context": "./migration/simulation/existing-ec2-agent",
                    "dockerfile": "Dockerfile",
                },
                "port": 8000,
                "invoke_path": "/run",
                "health_path": "/healthz",
                "trigger": "webhook",
                "env": {},
                "secrets": ["JIRA_TOKEN", "GIT_TOKEN"],
            },
            "target": {"runtime": "agentcore", "mode": "adapter"},
            "network": {"private_dependencies": [], "connectivity": "none"},
            "stages": {
                "runtime": {},
                "data": {"strategy": "none"},
                "triggers": {"strategy": "none"},
                "traffic": {"strategy": "none"},
            },
        },
    ),
    "multi-agent": _journey(
        {
            "pattern": "claude-sdk-multi-agent",
            "a2a": True,
            "memory": {"long_term": True, "top_k": 10, "relevance_score": 0.3},
        }
    ),
}

TOPOLOGIES: dict[str, dict] = {
    "centralized": {"deployment": {"strategy": "centralized"}},
    "distributed": {
        "deployment": {"strategy": "distributed", "platform_account": "000000000000"}
    },
    "federated": {
        "deployment": {
            "strategy": "federated",
            "platform_account": "000000000000",
            "workload_accounts": ["123456789012"],
            "federation": {
                "gateway_url": (
                    "https://REPLACE_ME.gateway.bedrock-agentcore"
                    ".us-east-1.amazonaws.com/mcp"
                ),
                "issuer_url": (
                    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_REPLACEME"
                ),
                "m2m_client_id": "REPLACE_ME",
                "m2m_client_secret_name": "agentcore/platform-m2m",  # pragma: allowlist secret
            },
        }
    },
}

POSTURES: dict[str, dict] = {
    "dev": {"deployment": {"mode": "workshop"}},
    "hardened": {
        "security": {
            "networking": True,
            "cloudtrail_alerting": True,
            "traceability": True,
        },
        "observability": {"alarms": True},
    },
    "production": {
        "project": "agentcore-production",
        "environment": "prod",
        "deployment": {"mode": "production", "platform_account": "000000000000"},
        "identity": {**_ENTRA_PLACEHOLDER_IDENTITY, "mode": "direct"},
        "agents": {
            "model_id": "us.anthropic.claude-sonnet-4-6",
            "allowed_models": ["us.anthropic.claude-sonnet-4-6"],
            "memory": {"event_expiry_days": 30},
        },
        "gateway": {"web_search": "off"},
        "security": {
            "networking": True,
            "cloudtrail_alerting": True,
            "resource_policies": True,
            "egress_filter": True,
            "require_guardrails": True,
            "cedar": {"enabled": True, "mode": "ENFORCE"},
            "traceability": True,
            "org_id": "o-example1234",
        },
        "observability": {
            "alarms": True,
            "alarm_email": "ops@REPLACE_ME.invalid",
            "log_retention_days": 90,
        },
    },
}

# One line per posture layer; scripts/gen_posture_tiers.py renders these into
# docs/POSTURE_TIERS.md so the documented names cannot drift from POSTURES.
POSTURE_DESCRIPTIONS: dict[str, str] = {
    "dev": (
        "Workshop lifecycle (`deployment.mode: workshop`): disposable resources, "
        "every security control opt-in."
    ),
    "hardened": (
        "Workshop lifecycle with VPC networking, CloudTrail alerting, "
        "traceability and alarms on; the other controls stay opt-in."
    ),
    "production": (
        "Production lifecycle: retained state and every control production "
        "validation requires, including an enterprise IdP, Cedar `ENFORCE`, "
        "guardrails, a model allow-list and alarms."
    ),
}

# The eight flat preset names, each a journey x topology x posture plus a
# small overlay for the keys that are not a pure product of the axes.
SHORTCUTS: dict[str, dict] = {
    "greenfield": {
        "journey": "greenfield",
        "topology": "centralized",
        "posture": "dev",
    },
    "migration": {"journey": "migration", "topology": "centralized", "posture": "dev"},
    "multi-agent": {
        "journey": "multi-agent",
        "topology": "centralized",
        "posture": "dev",
    },
    "distributed": {
        "journey": "greenfield",
        "topology": "distributed",
        "posture": "dev",
    },
    "federated": {
        "journey": "greenfield",
        "topology": "federated",
        "posture": "dev",
        "overlay": {
            "identity": {**_ENTRA_PLACEHOLDER_IDENTITY, "mode": "direct"},
            "agents": {"pattern": "strands-agent"},
        },
    },
    "platform-team": {
        "journey": "greenfield",
        "topology": "federated",
        "posture": "hardened",
        "overlay": {"agents": {"a2a": True, "memory": {"long_term": True}}},
    },
    "security-focused": {
        "journey": "greenfield",
        "topology": "centralized",
        "posture": "hardened",
        "overlay": {
            "agents": {
                "model_id": "us.anthropic.claude-sonnet-4-6",
                "allowed_models": ["us.anthropic.claude-sonnet-4-6"],
            },
            "security": {
                "resource_policies": True,
                "egress_filter": True,
                "require_guardrails": True,
                "cedar": {"enabled": True, "mode": "LOG_ONLY"},
                "org_id": "",
            },
            "observability": {"alarms": False},
        },
    },
    "production": {
        "journey": "greenfield",
        "topology": "centralized",
        "posture": "production",
        "overlay": {"agents": {"pattern": "strands-agent"}},
    },
}

# Migration x production is refused rather than wired through. The migration
# adapter only proxies to the customer container and never calls Bedrock
# itself, so there is no guardrail id it could attach. Production requires
# security.require_guardrails, whose DenyUngovernedInference statement on the
# runtime role would block every guardrail-less model call the container makes.
MIGRATION_PRODUCTION_REASON = (
    "the migration adapter only proxies to the customer container, whose own "
    "Bedrock calls carry no platform guardrail, so the runtime role's "
    "DenyUngovernedInference statement (required by production) would block "
    "every model call; rehearse the migration with deployment.mode workshop "
    "(docs/MIGRATION_RUNBOOK.md, 'Production mode')"
)


def migration_production_error(mode: str, migration_enabled: bool) -> str | None:
    """The refusal message for migration with production mode, else None."""
    if mode == "production" and migration_enabled:
        return (
            "migration with deployment.mode 'production' is refused: "
            + MIGRATION_PRODUCTION_REASON
        )
    return None


# Cedar permits shipped for the gateway and the tools they cover. In ENFORCE
# mode Cedar's implicit default-deny blocks every other tool.
CEDAR_PERMIT_CONTROL = "control-library/cedar/gateway-default"
CEDAR_PERMITTED_TOOLS = frozenset({"sample-tool"})
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def known_gateway_tools(tools_dir: Path = TOOLS_DIR) -> list[str]:
    """Bare gateway.tools names: sample-tool plus every tools/<dir> that
    ships a tool_spec.json (underscores become dashes)."""
    names = {"sample-tool"}
    if tools_dir.is_dir():
        for spec in tools_dir.glob("*/tool_spec.json"):
            names.add(spec.parent.name.replace("_", "-"))
    return sorted(names)


def gateway_tool_errors(tools: list, known: list[str] | None = None) -> list[str]:
    """Errors for an empty gateway.tools list or unknown bare tool names."""
    if not tools:
        return [
            (
                "gateway.tools is empty: list at least one tool (for example "
                "[sample-tool]) or omit the key for the sample-tool default"
            )
        ]
    known = known if known is not None else known_gateway_tools()
    unknown = [t for t in tools if isinstance(t, str) and t not in known]
    if unknown:
        return [
            (
                f"gateway.tools: unknown tool name(s) {', '.join(unknown)}; known "
                f"names: {', '.join(known)} (declare any other target as a block "
                "with name and type)"
            )
        ]
    return []


def cedar_findings(
    *, enabled: bool, mode: str, tool_names: list[str], production: bool
) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the Cedar settings against the gateway tools.

    ENFORCE without the engine is an error. ENFORCE with tools no shipped
    permit covers is an error in production and a warning in workshop.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if mode == "ENFORCE" and not enabled:
        errors.append(
            "security.cedar.mode ENFORCE requires security.cedar.enabled: true; "
            "with the policy engine disabled the setting does nothing"
        )
    if enabled and mode == "ENFORCE":
        uncovered = sorted(set(tool_names) - CEDAR_PERMITTED_TOOLS)
        if uncovered:
            message = (
                "security.cedar.mode ENFORCE denies every gateway tool without a "
                f"Cedar permit: {', '.join(uncovered)}. The shipped permits in "
                f"{CEDAR_PERMIT_CONTROL} cover only "
                f"{', '.join(sorted(CEDAR_PERMITTED_TOOLS))}; add a permit there "
                "for each tool, remove the tools, or keep mode LOG_ONLY"
            )
            (errors if production else warnings).append(message)
    return errors, warnings


# Partial combinations (any subset of journey/topology/posture/runner) that
# are refused outright, each with the reason shown to the user.
REFUSE: list[dict] = [
    {
        "when": {"posture": "production", "runner": "workshop"},
        "reason": (
            "the production profile is design/build only and refuses the "
            "guided workshop runner"
        ),
    },
    {
        "when": {"journey": "multi-agent", "posture": "production"},
        "reason": (
            "the multi-agent journey uses a claude-sdk-* pattern, which cannot "
            "attach the Bedrock Guardrail the production posture requires"
        ),
    },
    {
        "when": {"journey": "migration", "posture": "production"},
        "reason": MIGRATION_PRODUCTION_REASON,
    },
]


def _deep_merge(base: dict, over: dict, *, keep_existing: bool = False) -> dict:
    """Recursive mapping merge; scalars and lists from ``over`` replace.

    With ``keep_existing`` a key already set in ``base`` is never replaced,
    only missing keys are filled (how the journey owns agents:).
    """
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value, keep_existing=keep_existing)
        elif keep_existing and key in out:
            continue
        else:
            out[key] = copy.deepcopy(value)
    return out


def _merge_layers(layers: list[dict]) -> dict:
    doc = copy.deepcopy(layers[0])
    for layer in layers[1:]:
        rest = {k: v for k, v in layer.items() if k != "agents"}
        doc = _deep_merge(doc, rest)
        if "agents" in layer:
            doc["agents"] = _deep_merge(
                doc.get("agents", {}), layer["agents"], keep_existing=True
            )
    return doc


def _layer(kind: str, table: dict[str, dict], name: str) -> dict:
    if name not in table:
        raise ValueError(
            f"unknown {kind} layer {name!r}; choose one of: {', '.join(table)}"
        )
    return table[name]


def refusal_reason(**choice: str | None) -> str | None:
    """The reason of the first REFUSE entry the choice matches, else None."""
    for entry in REFUSE:
        if all(choice.get(k) == v for k, v in entry["when"].items()):
            return entry["reason"]
    return None


def _compose(
    journey: str,
    topology: str,
    posture: str,
    region: str | None,
    runner: str | None,
    overlay: dict | None,
) -> dict:
    layers = [
        _layer("journey", JOURNEYS, journey),
        _layer("topology", TOPOLOGIES, topology),
        _layer("posture", POSTURES, posture),
    ]
    reason = refusal_reason(
        journey=journey, topology=topology, posture=posture, runner=runner
    )
    if reason:
        combo = f"journey={journey} topology={topology} posture={posture}"
        if runner:
            combo += f" runner={runner}"
        raise ValueError(f"refused ({combo}): {reason}")
    doc = _merge_layers(layers)
    if overlay:
        doc = _deep_merge(doc, overlay)
    if region:
        doc["region"] = region
    return doc


def compose_layers(
    journey: str,
    topology: str,
    posture: str,
    region: str | None = None,
    runner: str | None = None,
) -> dict:
    """Compose one platform.yaml document from the three preset layers.

    Raises ValueError naming an unknown layer or the matched refusal reason.
    The result is unvalidated: run it through validate_composed (or write it
    and let `deploy.sh design` validate it as today).
    """
    return _compose(journey, topology, posture, region, runner, None)


def resolve_shortcut(
    name: str, region: str | None = None, runner: str | None = None
) -> dict:
    """Compose the design one of the eight legacy preset names stands for."""
    if name not in SHORTCUTS:
        raise ValueError(
            f"unknown shortcut {name!r}; choose one of: {', '.join(SHORTCUTS)}"
        )
    s = SHORTCUTS[name]
    return _compose(
        s["journey"], s["topology"], s["posture"], region, runner, s.get("overlay")
    )


def validate_composed(doc: dict) -> PlatformConfig:
    """The same model validation every platform.yaml goes through."""
    return PlatformConfig.model_validate(doc)


def compose_cli(argv: list[str]) -> int:
    """deploy.sh entry: PATH JOURNEY TOPOLOGY POSTURE [REGION] -> platform.yaml."""
    path, journey, topology, posture, *rest = argv
    region = rest[0] if rest else ""
    try:
        doc = compose_layers(journey, topology, posture, region=region or None)
    except ValueError as exc:
        print(exc)
        return 1
    header = (
        f"# Generated from presets/ layers by deploy.sh (journey={journey}, "
        f"topology={topology}, posture={posture}).\n"
        "# Safe to edit; this header only marks that design may regenerate "
        "the file.\n"
    )
    Path(path).write_text(header + yaml.safe_dump(doc, sort_keys=False))
    print(
        f"Layers journey={journey} topology={topology} posture={posture} "
        f"materialized as {path}"
    )
    return 0


class FederationConfig(BaseModel):
    """Platform-account endpoints a federated WORKLOAD account consumes.

    The platform team fills this block after deploying the platform account
    and hands it to workload teams (all four values are outputs of the auth
    and gateway stacks; none is secret — the M2M client secret itself goes
    into the workload account's OWN Secrets Manager under
    m2m_client_secret_name, never into this file).

    Trust is pure OAuth, verified live (docs/MULTI_ACCOUNT.md): the workload
    account's token vault exchanges these client credentials against the
    platform Cognito token endpoint, and the platform gateway accepts the
    resulting JWT. No cross-account IAM anywhere on the data plane.
    """

    gateway_url: str = ""
    issuer_url: str = ""  # platform Cognito issuer; discovery URL is derived
    m2m_client_id: str = ""
    m2m_client_secret_name: str = ""  # Secrets Manager NAME in the workload account

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer_url.rstrip('/')}/.well-known/openid-configuration"

    @property
    def is_complete(self) -> bool:
        return all(
            (
                self.gateway_url,
                self.issuer_url,
                self.m2m_client_id,
                self.m2m_client_secret_name,
            )
        )


class DeploymentConfig(BaseModel):
    """Deployment posture and multi-account strategy. See docs/MULTI_ACCOUNT.md.

    mode — workshop preserves the accelerator's disposable defaults;
        production enables retained state and requires the controls enforced
        by PlatformConfig._production_controls_are_complete. This key is the
        source of truth: a differing DEPLOYMENT_MODE / -c deployment_mode is
        refused unless -c allow_mode_override=true (resolve_deployment_mode).
    centralized — everything in platform_account (the default).
    distributed — each team/workload account runs its own full copy of this
        file; platform_account binds that copy to its intended account and org
        guardrails (control-library/terraform/org-guardrails) apply org-wide.
    federated — shared services (auth, gateway, observability account setting)
        live in platform_account; workload_accounts run agent runtimes plus
        their own credential provider, consuming the platform gateway via
        OAuth. The account you deploy into decides what gets deployed: the
        same file works in both.
    """

    mode: Literal["workshop", "production"] = "workshop"
    strategy: Literal["centralized", "distributed", "federated"] = "centralized"
    platform_account: str = ""
    workload_accounts: list[str] = Field(default_factory=list)
    federation: FederationConfig = Field(default_factory=FederationConfig)

    @field_validator("platform_account")
    @classmethod
    def _platform_account_shape(cls, v: str) -> str:
        if v:
            _account_shape("deployment.platform_account", v)
        return v

    @field_validator("workload_accounts")
    @classmethod
    def _workload_account_shapes(cls, v: list[str]) -> list[str]:
        for i, a in enumerate(v):
            _account_shape(f"deployment.workload_accounts[{i}]", a)
        return v

    @model_validator(mode="after")
    def _federated_needs_accounts(self) -> DeploymentConfig:
        if self.strategy == "federated":
            if not self.platform_account:
                raise ValueError(
                    "strategy 'federated' requires deployment.platform_account"
                )
            if not self.workload_accounts:
                raise ValueError(
                    "strategy 'federated' requires deployment.workload_accounts"
                )
        # One account cannot be both sides: federated_role() would call it
        # "platform" and silently deploy no agent runtimes there.
        both = sorted(set(self.workload_accounts) & {self.platform_account})
        if both:
            raise ValueError(
                f"account {both[0]} is both deployment.platform_account and in "
                "deployment.workload_accounts — an account is one side of the "
                "federation, not both"
            )
        _refuse_placeholders(deployment_placeholders(self))
        return self


def _account_placeholder_msg(field: str, value: str) -> str:
    return (
        f"{field} is a placeholder ({value!r}): put the 12-digit AWS account id "
        "here (aws sts get-caller-identity --query Account)"
    )


def _account_shape(field: str, v: str) -> None:
    # A word-shaped placeholder (REPLACE_ME) can never synthesize, so it is a
    # hard error even for the parity gate; the digit-shaped sentinels below
    # pass here and are handled by deployment_placeholders().
    if _SENTINEL_RE.search(v):
        raise ValueError(_account_placeholder_msg(field, v))
    if not re.fullmatch(_ACCOUNT_RE, v):
        raise ValueError(f"{field}: not a 12-digit AWS account id: {v!r}")


def deployment_placeholders(deployment: DeploymentConfig) -> list[str]:
    """Sentinel account ids (see the Placeholders block above)."""
    named = [("deployment.platform_account", deployment.platform_account)]
    named += [
        (f"deployment.workload_accounts[{i}]", a)
        for i, a in enumerate(deployment.workload_accounts)
    ]
    out = [
        _account_placeholder_msg(field, value)
        for field, value in named
        if value in _ACCOUNT_SENTINELS
    ]
    # The federation block is what the platform team hands the workload teams;
    # a preset ships it as placeholders so the shape is visible. Any sentinel
    # left in place means the hand-over has not happened.
    fed = deployment.federation
    for field, value in (
        ("deployment.federation.gateway_url", fed.gateway_url),
        ("deployment.federation.issuer_url", fed.issuer_url),
        ("deployment.federation.m2m_client_id", fed.m2m_client_id),
    ):
        if value and _SENTINEL_RE.search(value):
            out.append(
                f"{field} is a placeholder ({value!r}): copy the value from the "
                "platform account's `deploy.sh export` output"
            )
    return out


# IdPs whose identity.mode resolves to direct when the key is omitted. Okta
# direct is supported (live evidence pending) but stays opt-in through an
# explicit mode: direct, so existing brokered Okta stacks do not switch.
# Ping has no direct mode.
_DIRECT_DEFAULT_IDPS = frozenset({"entra_id"})


class IdentityConfig(BaseModel):
    """Who issues the tokens the platform trusts.

    Omitted mode resolves per IdP: direct for entra_id, brokered for cognito,
    okta and ping. An explicit mode is always honoured unchanged.

    brokered — Cognito is the issuer; a corporate IdP, when set,
        is federated THROUGH it (users sign in at the IdP, Cognito mints the
        token). One issuer whatever the IdP, platform-owned M2M clients,
        works with no IdP at all.
    direct — the corporate IdP IS the issuer; no Cognito user pool is
        deployed. Gateway and runtimes validate the IdP's tokens by audience
        (Entra tokens carry `aud`/`azp`, not Cognito's `client_id`), and the
        M2M credential is the same app registration's client secret.
        Supported for entra_id and okta; ping stays brokered for now. Okta
        needs a custom authorization server (issuer path /oauth2/<id>), its
        audience (`audience`) and a custom scope for machine tokens
        (`m2m_scope`).
    """

    idp: Literal["cognito", "entra_id", "okta", "ping"] = "cognito"
    mode: Literal["brokered", "direct"] = "brokered"
    tenant_id: str = ""
    client_id: str = ""
    issuer_url: str = ""
    # Secrets Manager secret NAME — the secret itself never goes in this file.
    client_secret_name: str = ""
    # Okta direct mode: the authorization server's audience (the token `aud`)
    # and the custom scope requested with client_credentials.
    audience: str = ""
    m2m_scope: str = ""
    # True when mode was omitted and resolved to direct from the IdP default.
    _mode_defaulted: bool = PrivateAttr(default=False)

    @property
    def direct_issuer_url(self) -> str:
        """The IdP's OIDC issuer when it is the token issuer itself (mode direct).

        Entra: the v2.0 issuer. The app registration must request v2 access
        tokens (api.requestedAccessTokenVersion = 2), or Entra signs tokens
        with the v1 issuer `https://sts.windows.net/<tenant>/` and every
        authorizer rejects them — checked live by scripts/check_identity.py.
        """
        if self.idp == "entra_id":
            return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        return self.issuer_url.rstrip("/")

    @property
    def direct_m2m_scope(self) -> str:
        """client_credentials scope in direct mode: the app's own `.default`,
        so the token's audience is the app itself (Entra rejects a missing
        scope outright). Okta: the custom scope defined on the authorization
        server."""
        if self.idp == "okta":
            return self.m2m_scope
        return f"{self.client_id}/.default"

    @property
    def direct_audience(self) -> str:
        """The `aud` the authorizers pin in direct mode: Okta tokens carry the
        authorization server's audience, Entra tokens the app's client id."""
        if self.idp == "okta":
            return self.audience
        return self.client_id

    @model_validator(mode="after")
    def _federated_idp_fields(self) -> IdentityConfig:
        if "mode" not in self.model_fields_set and self.idp in _DIRECT_DEFAULT_IDPS:
            self.mode = "direct"
            self._mode_defaulted = True
        if self.mode == "direct" and self.idp not in ("entra_id", "okta"):
            raise ValueError(
                f"identity.mode 'direct' is supported for idp 'entra_id' and "
                f"'okta' today; idp {self.idp!r} deploys brokered (through "
                "Cognito). Set mode: brokered or switch the IdP."
            )
        if self.idp == "entra_id" and not self.tenant_id:
            raise ValueError("idp 'entra_id' requires identity.tenant_id")
        if self.idp in ("okta", "ping") and not self.issuer_url:
            raise ValueError(f"idp {self.idp!r} requires identity.issuer_url")
        if self.idp != "cognito" and not self.client_secret_name:
            raise ValueError(
                f"idp {self.idp!r} requires identity.client_secret_name "
                "(a Secrets Manager secret name — never the secret itself)"
            )
        if self.idp != "cognito" and not self.client_id:
            raise ValueError(
                f"idp {self.idp!r} requires identity.client_id (the IdP app "
                "registration's client id)"
            )
        if self.mode == "direct" and self.idp == "okta":
            errors = _okta_direct_errors(self)
            if errors:
                raise ValueError("\n".join(errors))
        _refuse_placeholders(identity_placeholders(self))
        # Every entry point (design, doctor, build via app.py) loads the
        # config through this model, so the upgrade warning is printed
        # before any stack can change.
        emit_identity_mode_warnings(self)
        return self


def _okta_direct_errors(identity: IdentityConfig) -> list[str]:
    """What Okta direct mode needs beyond the brokered fields."""
    errors: list[str] = []
    if not re.search(r"/oauth2/[^/]+", urlparse(identity.issuer_url).path):
        errors.append(
            f"identity.issuer_url {identity.issuer_url!r} is not an Okta custom "
            "authorization server: direct mode needs "
            "https://<org>.okta.com/oauth2/default or /oauth2/<authServerId> "
            "(Okta admin > Security > API > Authorization Servers > Issuer URI). "
            "Tokens from the org authorization server (https://<org>.okta.com) "
            "are only for Okta's own APIs and cannot be validated by third parties"
        )
    if not identity.audience:
        errors.append(
            "idp 'okta' with mode 'direct' requires identity.audience (Okta "
            "admin > Security > API > Authorization Servers > <server> > "
            "Settings > Audience, e.g. api://default)"
        )
    if not identity.m2m_scope:
        errors.append(
            "idp 'okta' with mode 'direct' requires identity.m2m_scope (a custom "
            "scope on that authorization server, Okta admin > Security > API > "
            "Authorization Servers > <server> > Scopes, e.g. agentcore.invoke)"
        )
    return errors


# Messages already printed in this process: one load path may validate the
# same identity block more than once.
_EMITTED_MODE_WARNINGS: set[str] = set()


def identity_mode_warnings(identity: IdentityConfig) -> list[str]:
    """Warn when an omitted identity.mode resolved to direct (the default
    used to be brokered for every IdP). An explicit IDP_MODE override in the
    environment decides the effective mode, so it does not warn."""
    if not identity._mode_defaulted or identity.mode != "direct":
        return []
    if os.environ.get("IDP_MODE"):
        return []
    return [
        (
            f"identity.mode is not set: it now resolves to 'direct' for idp "
            f"{identity.idp!r}. A stack deployed earlier in brokered mode will "
            "change on the next build unless platform.yaml pins "
            "`identity.mode: brokered`."
        )
    ]


def emit_identity_mode_warnings(identity: IdentityConfig) -> None:
    """Print identity_mode_warnings on stderr, once per message per process."""
    for message in identity_mode_warnings(identity):
        if message in _EMITTED_MODE_WARNINGS:
            continue
        _EMITTED_MODE_WARNINGS.add(message)
        print(f"[WARN] {message}", file=sys.stderr)


def identity_placeholders(identity: IdentityConfig) -> list[str]:
    """Sentinel IdP values (see the Placeholders block above). Empty fields
    are not placeholders — whether they are REQUIRED is the model's job."""
    out: list[str] = []
    t = identity.tenant_id
    if t and (not _UUID_RE.match(t) or set(t) <= set("0-")):
        out.append(
            f"identity.tenant_id is a placeholder ({t!r}): put your Entra tenant "
            "(directory) id here — the UUID under Entra admin center > Overview"
        )
    c = identity.client_id
    if c and _SENTINEL_RE.search(c):
        out.append(
            f"identity.client_id is a placeholder ({c!r}): put your IdP app "
            "registration's client id here"
        )
    u = identity.issuer_url
    if u and (not u.startswith("https://") or _SENTINEL_RE.search(u)):
        out.append(
            f"identity.issuer_url is a placeholder ({u!r}): put your IdP's https "
            "issuer URL here (Okta: https://<org>.okta.com/oauth2/default)"
        )
    a = identity.audience
    if a and _SENTINEL_RE.search(a):
        out.append(
            f"identity.audience is a placeholder ({a!r}): put your Okta "
            "authorization server's audience here (Security > API > "
            "Authorization Servers > Settings > Audience)"
        )
    m = identity.m2m_scope
    if m and _SENTINEL_RE.search(m):
        out.append(
            f"identity.m2m_scope is a placeholder ({m!r}): put the custom scope "
            "defined on your Okta authorization server here (Scopes tab)"
        )
    s = identity.client_secret_name
    if s and (
        _SENTINEL_RE.search(s)
        or not _SECRET_NAME_RE.match(s)
        or _JWT_RE.match(s)
        or _BLOB_RE.match(s)
    ):
        # The value is deliberately not echoed: it may BE the secret.
        out.append(
            "identity.client_secret_name is a placeholder or looks like a secret "
            "VALUE (not shown): it must be the Secrets Manager NAME of a secret "
            "you created, e.g. agentcore/idp-client-secret"
        )
    return out


def to_env(config: PlatformConfig) -> dict[str, str]:
    """platform.yaml -> the environment names deploy.sh and app.py read (see
    _base_env), plus identity.audience / identity.m2m_scope as IDP_AUDIENCE /
    IDP_M2M_SCOPE when set, so `deploy.sh verify` and
    scripts/check_identity.py get the Okta direct-mode values from the file."""
    env = _base_env(config)
    if config.identity.audience:
        env["IDP_AUDIENCE"] = config.identity.audience
    if config.identity.m2m_scope:
        env["IDP_M2M_SCOPE"] = config.identity.m2m_scope
    return env


class MemoryConfig(BaseModel):
    long_term: bool = False
    event_expiry_days: int = Field(default=30, ge=1, le=365)
    top_k: int = Field(default=10, ge=1, le=100)
    relevance_score: float = Field(default=0.3, ge=0.0, le=1.0)


AGENT_PATTERNS = (
    "orchestrator",
    "strands-agent",
    "langgraph-agent",
    "claude-sdk-agent",
    "claude-sdk-multi-agent",
    "agui-strands-agent",
    "agui-langgraph-agent",
)


class AgentsConfig(BaseModel):
    pattern: Literal[AGENT_PATTERNS] = "orchestrator"  # type: ignore[valid-type]
    model_id: str = ""  # empty = the pattern's own default
    # Generation 1 preserves the historical physical names. Increment this
    # only for a controlled replacement when AgentCore can no longer update or
    # even read a runtime (for example, its OIDC discovery endpoint was
    # deleted). Keeping it in the manifest makes later deploys converge on the
    # replacement instead of silently trying to recreate the broken name.
    orchestrator_runtime_generation: int = Field(default=1, ge=1, le=99)
    # Empty = no restriction: the runtime roles keep wildcard Bedrock IAM and
    # any MODEL_ID works — today's behavior, byte-identical templates.
    allowed_models: list[str] = Field(default_factory=list)
    a2a: bool = False
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @field_validator("allowed_models")
    @classmethod
    def _allowed_model_shapes(cls, v: list[str]) -> list[str]:
        # A malformed or placeholder entry would render into the runtime
        # role's IAM policy as an ARN that matches nothing — every model
        # invoke fails at runtime with AccessDenied, long after validation
        # said the config was fine. Catch it here instead.
        for m in v:
            if re.search(
                r"replace|example|changeme", m, re.IGNORECASE
            ) or not re.fullmatch(
                r"([a-z]{2,4}\.)?[a-z0-9-]+\.[a-z0-9][a-zA-Z0-9.:_-]*", m
            ):
                raise ValueError(
                    f"not a Bedrock model id: {m!r}. Expected provider.model or a "
                    "cross-region inference profile like "
                    "'us.anthropic.claude-sonnet-4-6'"
                )
        return v

    @model_validator(mode="after")
    def _allowlist_needs_model_id(self) -> AgentsConfig:
        if self.allowed_models:
            if not self.model_id:
                # When model_id is empty, MODEL_ID is never injected and each
                # agent container falls back to its baked-in DEFAULT_MODEL_ID —
                # the allow-list would be silently bypassed. Setting model_id
                # injects MODEL_ID into every runtime (including the A2A
                # sub-agents), closing the bypass.
                raise ValueError(
                    "agents.allowed_models requires agents.model_id: without it "
                    "each agent falls back to its baked-in DEFAULT_MODEL_ID and "
                    "the allow-list is silently bypassed"
                )
            if self.model_id not in self.allowed_models:
                raise ValueError(
                    f"agents.model_id {self.model_id!r} is not in "
                    f"agents.allowed_models {self.allowed_models}"
                )
        return self


# An existing AgentCore Identity credential provider in a token vault.
_PROVIDER_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z]{2}(-[a-z]+)+-\d:\d{12}:"
    r"token-vault/[A-Za-z0-9_-]+/"
    r"(?P<kind>apikeycredentialprovider|oauth2credentialprovider)/[A-Za-z0-9_.-]+$"
)
_PROVIDER_KIND = {
    "api_key": "apikeycredentialprovider",
    "oauth": "oauth2credentialprovider",
}


def read_openapi_schema(path: str) -> dict:
    """Parse a local OpenAPI file (JSON by suffix, YAML otherwise)."""
    text = Path(path).read_text()
    doc = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    if not isinstance(doc, dict) or not ({"openapi", "swagger"} & set(doc)):
        raise ValueError("not an OpenAPI document (no top-level 'openapi' key)")
    return doc


class CredentialProviderConfig(BaseModel):
    """Outbound auth for an OpenAPI target: an existing provider, by ARN."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["api_key", "oauth"]
    arn: str
    scopes: list[str] = Field(default_factory=list)

    @field_validator("arn")
    @classmethod
    def _arn_shape(cls, v: str) -> str:
        if not _PROVIDER_ARN_RE.fullmatch(v):
            raise ValueError(
                f"not an AgentCore Identity credential provider ARN: {v!r}. "
                "Expected arn:aws:bedrock-agentcore:<region>:<account>:token-vault/"
                "<vault>/(apikeycredentialprovider|oauth2credentialprovider)/<name>"
            )
        return v

    @model_validator(mode="after")
    def _kind_matches_type(self) -> CredentialProviderConfig:
        errors: list[str] = []
        kind = _PROVIDER_ARN_RE.fullmatch(self.arn).group("kind")
        if kind != _PROVIDER_KIND[self.type]:
            errors.append(
                f"credential_provider.type {self.type!r} needs a "
                f"{_PROVIDER_KIND[self.type]} ARN, got a {kind} ARN"
            )
        if self.scopes and self.type != "oauth":
            errors.append("credential_provider.scopes apply only to type 'oauth'")
        if errors:
            raise ValueError("\n".join(errors))
        return self


class GatewayTarget(BaseModel):
    """One typed gateway target. Relative paths resolve from the repo root."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*$", max_length=48)
    type: Literal["lambda", "openapi"]
    # lambda: handler directory (default tools/<name>) and its tool schema
    # (default <source_dir>/tool_spec.json, the CloudFormation PascalCase list).
    source_dir: str = ""
    tool_spec: str = ""
    env_vars: dict[str, str] = Field(default_factory=dict)
    # openapi: local spec inlined at synth, plus the outbound credential.
    schema_path: str = ""
    credential_provider: CredentialProviderConfig | None = None

    @model_validator(mode="after")
    def _fields_match_type(self) -> GatewayTarget:
        where = f"gateway target {self.name!r}"
        errors: list[str] = []
        if self.type == "lambda":
            for field in ("schema_path", "credential_provider"):
                if getattr(self, field):
                    errors.append(f"{where}: {field} is only for type 'openapi'")
            self.source_dir = self.source_dir or f"tools/{self.name.replace('-', '_')}"
            self.tool_spec = self.tool_spec or f"{self.source_dir}/tool_spec.json"
            if not Path(self.tool_spec).is_file():
                errors.append(
                    f"{where}: tool_spec {self.tool_spec!r} is not a file "
                    "(the Lambda's tool schema)"
                )
        else:
            for field in ("source_dir", "tool_spec", "env_vars"):
                if getattr(self, field):
                    errors.append(f"{where}: {field} is only for type 'lambda'")
            if not self.schema_path:
                errors.append(f"{where}: type 'openapi' requires schema_path")
            elif not Path(self.schema_path).is_file():
                errors.append(
                    f"{where}: schema_path {self.schema_path!r} is not a file"
                )
            else:
                try:
                    read_openapi_schema(self.schema_path)
                except (ValueError, yaml.YAMLError) as exc:
                    errors.append(f"{where}: schema_path {self.schema_path!r}: {exc}")
            if self.credential_provider is None:
                errors.append(
                    f"{where}: type 'openapi' requires credential_provider "
                    "(type and arn of an AgentCore Identity credential provider)"
                )
        if errors:
            raise ValueError("\n".join(errors))
        return self


def gateway_tool_configs(
    tools: list[str | GatewayTarget], defaults: dict[str, dict]
) -> dict[str, dict]:
    """gateway.tools -> the tool_configs mapping GatewayStack consumes.

    A bare name in ``defaults`` (sample-tool) is passed through unchanged; any
    other bare name is a Lambda under tools/<name> with its tool_spec.json.
    """
    configs: dict[str, dict] = {}
    for entry in tools:
        if isinstance(entry, str):
            if entry in defaults:
                configs[entry] = copy.deepcopy(defaults[entry])
                continue
            entry = GatewayTarget(name=entry, type="lambda")
        if entry.type == "lambda":
            spec = json.loads(Path(entry.tool_spec).read_text())
            if not isinstance(spec, list):
                raise ValueError(
                    f"{entry.tool_spec}: tool_spec must be a JSON list of tools"
                )
            configs[entry.name] = {
                "type": "lambda",
                "source_dir": entry.source_dir,
                "env_vars": dict(entry.env_vars),
                "tool_schema": spec,
            }
        else:
            configs[entry.name] = {
                "type": "openapi",
                "openapi_schema": json.dumps(read_openapi_schema(entry.schema_path)),
                "credential_provider": entry.credential_provider.model_dump(),
            }
    return configs


class GatewayConfig(BaseModel):
    # auto = on where the built-in connector exists (WEB_SEARCH_REGIONS).
    web_search: Literal["auto", "on", "off"] = "auto"
    # A bare name is a Lambda tool (the sample-tool default or tools/<name>/);
    # a block is a typed target (lambda or openapi), see GatewayTarget.
    tools: list[str | GatewayTarget] = Field(default_factory=lambda: ["sample-tool"])

    @field_validator("web_search", mode="before")
    @classmethod
    def _yaml_boolean_web_search(cls, v):
        # PyYAML follows YAML 1.1 and parses bare `on` / `off` as booleans.
        # Preserve the operator's intended enum instead of rejecting a common,
        # visually correct manifest spelling.
        if isinstance(v, bool):
            return "on" if v else "off"
        return v

    @model_validator(mode="after")
    def _unique_target_names(self) -> GatewayConfig:
        names = [t if isinstance(t, str) else t.name for t in self.tools]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(
                f"gateway.tools: duplicate target names {dupes}; each target "
                "needs a unique name"
            )
        return self


class CedarConfig(BaseModel):
    enabled: bool = False
    mode: Literal["LOG_ONLY", "ENFORCE"] = "LOG_ONLY"


class SecurityConfig(BaseModel):
    networking: bool = False  # VPC mode for the runtimes
    cloudtrail_alerting: bool = False  # the security stack (trail + alerting)
    resource_policies: bool = False
    egress_filter: bool = False
    # IAM-denies ungoverned inference on the runtime roles; creates a baseline
    # guardrail per runtime and injects it into the agents.
    require_guardrails: bool = False
    cedar: CedarConfig = Field(default_factory=CedarConfig)
    traceability: bool = False
    org_id: str = ""

    @field_validator("org_id")
    @classmethod
    def _org_id_shape(cls, v: str) -> str:
        # Shape is always enforced. Sentinel values with a valid shape are
        # warnings only for the parity gate and remain hard errors on deploy.
        if v and not re.fullmatch(r"o-[a-z0-9]{10,32}", v):
            raise ValueError(
                f"not an AWS Organizations id: {v!r}. Find yours with: "
                "aws organizations describe-organization --query Organization.Id"
            )
        _refuse_placeholders(security_org_placeholders(v))
        return v

    @model_validator(mode="after")
    def _traceability_needs_trail(self) -> SecurityConfig:
        # The EventBridge alerting only fires if CloudTrail management events
        # are recorded — silently useless without the trail (TESTING.md caveat 5).
        if self.traceability and not self.cloudtrail_alerting:
            raise ValueError(
                "security.traceability needs security.cloudtrail_alerting: the "
                "alert rule only fires on CloudTrail management events"
            )
        return self


def security_org_placeholders(org_id: str) -> list[str]:
    if org_id and re.search(r"replace|example|changeme", org_id, re.IGNORECASE):
        return [
            (
                f"security.org_id is a placeholder ({org_id!r}): find yours with "
                "`aws organizations describe-organization --query Organization.Id`"
            )
        ]
    return []


class ObservabilityConfig(BaseModel):
    transaction_search: bool = True
    # CloudWatch alarms + SNS ops topic + the platform dashboard.
    alarms: bool = False
    alarm_email: str = ""  # empty = topic exists, no email subscription
    log_retention_days: int = 30

    @field_validator("log_retention_days")
    @classmethod
    def _log_retention_supported(cls, v: int) -> int:
        if v not in {30, 90, 180, 365}:
            raise ValueError("must be one of 30, 90, 180, or 365 days")
        return v

    @field_validator("alarm_email")
    @classmethod
    def _alarm_email_shape(cls, v: str) -> str:
        # Empty is fine — the topic still deploys for manual subscription.
        # A placeholder is not: SNS mails a confirmation link to an inbox
        # nobody reads and every alarm after that goes nowhere.
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError(
                f"not a subscribable email address: {v!r}. Use a real inbox "
                "or leave it empty."
            )
        _refuse_placeholders(observability_placeholders(v))
        return v


def observability_placeholders(alarm_email: str) -> list[str]:
    if alarm_email and _SENTINEL_RE.search(alarm_email):
        return [
            (
                "observability.alarm_email is a placeholder: use a real monitored "
                "inbox; SNS confirmation and alarm delivery must reach an operator"
            )
        ]
    return []


@dataclass(frozen=True)
class RegionFinding:
    level: Literal["error", "warning"]
    code: str
    message: str

    def render(self) -> str:
        prefix = "[ERROR]" if self.level == "error" else "[WARN]"
        return f"{prefix} {self.message}"


def _config_region(config) -> str:
    region = getattr(config, "region", "") or ""
    if not region:
        region = getattr(getattr(config, "project", None), "region", "") or ""
    return region


def _split_profile(model_id: str) -> tuple[str, str]:
    head, sep, rest = model_id.partition(".")
    if sep and head in INFERENCE_PROFILES_BY_GEO:
        return head, rest
    return "", model_id


def region_findings(config, region: str | None = None) -> list[RegionFinding]:
    """Static, region-aware design checks. Never calls AWS.

    `config` needs `security.networking`, `agents.model_id`,
    `agents.allowed_models` and `gateway.web_search`. The Region is taken from
    `region` or, failing that, from the config. Every message names it.
    """
    # Lazy import keeps platform_config importable without a cycle; infra_utils
    # must never import stacks (scripts/check_repository_structure.py).
    from infra_utils.runtime_network import SUPPORTED_ZONE_IDS

    region = region or _config_region(config)
    out: list[RegionFinding] = []

    # A missing entry is not proof the Region is unsupported: the runtime
    # helper deliberately applies no zone restriction for unknown Regions so a
    # stale table never blocks a Region AgentCore has since added. Warn only.
    if config.security.networking and region not in SUPPORTED_ZONE_IDS:
        out.append(
            RegionFinding(
                "warning",
                "zones-unknown",
                f"security.networking is on but region {region} has no "
                "SUPPORTED_ZONE_IDS entry (infra_utils/runtime_network.py), so "
                "AgentCore zone support could not be checked statically; no "
                f"zone restriction is applied. Run doctor in {region} to confirm",
            )
        )

    agents = config.agents
    models = [m for m in [agents.model_id, *agents.allowed_models] if m]
    models = list(dict.fromkeys(models))
    geo = REGION_GEO.get(region)
    if models and geo is None:
        out.append(
            RegionFinding(
                "warning",
                "model-region-unknown",
                f"model availability could not be checked: region {region} is "
                "not in REGION_GEO",
            )
        )
    elif geo is not None:
        known = set().union(*INFERENCE_PROFILES_BY_GEO.values())
        for model in models:
            prefix, base = _split_profile(model)
            if base not in known:
                out.append(
                    RegionFinding(
                        "warning",
                        "model-unknown",
                        f"model {model} is not a known cross-region inference "
                        f"profile in {region}",
                    )
                )
            elif prefix and prefix not in ("global", geo):
                out.append(
                    RegionFinding(
                        "warning",
                        "model-geo-mismatch",
                        f"model {model} uses the '{prefix}.' profile but region "
                        f"{region} serves '{geo}.' (or 'global.') profiles",
                    )
                )
            elif prefix and base not in INFERENCE_PROFILES_BY_GEO[prefix]:
                out.append(
                    RegionFinding(
                        "warning",
                        "model-unknown",
                        f"model {model} is not a known cross-region inference "
                        f"profile in {region}",
                    )
                )

    if config.gateway.web_search == "on" and region not in WEB_SEARCH_REGIONS:
        out.append(
            RegionFinding(
                "warning",
                "web-search-region",
                f"gateway.web_search is on but region {region} is not a Web "
                f"Search launch region ({', '.join(sorted(WEB_SEARCH_REGIONS))})",
            )
        )
    return out


# ── Migration ──
# An existing agent (someone else's container) moves onto the platform. The
# block describes where it comes from, how the platform should run it, and
# what it must still reach privately — the contract the adapter (M2), the EC2
# target (M3) and the docs consume. Contract-only here: app.py reads the
# to_env() names once those land.
_ENV_NAME_RE = r"^[A-Z][A-Z0-9_]*$"
_SECRETISH_KEY_RE = re.compile(r"secret|password|token|key$", re.IGNORECASE)
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


class MigrationBuild(BaseModel):
    context: str  # docker build context, relative to the repo root
    dockerfile: str = "Dockerfile"  # relative to context

    model_config = {"extra": "forbid"}
    _compatibility_warning: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _normalize_legacy_simulation_context(self) -> MigrationBuild:
        for legacy in ("./workshop-simulation", "workshop-simulation"):
            if self.context == legacy or self.context.startswith(f"{legacy}/"):
                suffix = self.context.removeprefix(legacy)
                self._compatibility_warning = (
                    f"migration.source.build.context {self.context!r} uses the "
                    "pre-v0.2 repository path; it was normalized to "
                    f"'./migration/simulation{suffix}'. Update platform.yaml."
                )
                self.context = f"./migration/simulation{suffix}"
                break
        return self


class MigrationSource(BaseModel):
    """The customer's agent as it runs today."""

    platform: Literal[
        "openshift", "kubernetes", "ec2", "ecs", "lambda", "on-prem", "other"
    ]
    image: str = ""  # pre-built image reference — XOR build
    build: MigrationBuild | None = None  # build here (yields arm64 for AgentCore)
    registry_secret_name: str = ""  # Secrets Manager NAME holding docker-login creds
    port: int | None = Field(default=None, ge=1, le=65535)
    invoke_path: str = ""  # where the container takes a request (adapter mode)
    health_path: str = "/"  # what the adapter polls for /ping
    trigger: Literal["webhook", "schedule", "http", "queue"] = "http"
    # Plain configuration. Anything secret-shaped is refused here on purpose:
    # env values render into the task definition / runtime config in clear.
    env: dict[str, str] = Field(default_factory=dict)
    # ENV NAMES only; each value lives in Secrets Manager under
    # <project>/<environment>/migration/<NAME> (see migration_plan()).
    secrets: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("invoke_path", "health_path")
    @classmethod
    def _paths_are_absolute(cls, v: str) -> str:
        if v and not v.startswith("/"):
            raise ValueError(f"must start with '/', got {v!r}")
        return v

    @field_validator("env")
    @classmethod
    def _env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.fullmatch(_ENV_NAME_RE, k):
                raise ValueError(
                    f"env key {k!r} is not an environment variable name "
                    "(UPPER_SNAKE_CASE)"
                )
            if _SECRETISH_KEY_RE.search(k):
                raise ValueError(
                    f"env key {k!r} looks like a secret; list its NAME under "
                    "migration.source.secrets instead and store the value in "
                    "Secrets Manager (env values are rendered in clear)"
                )
        return v

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if not re.fullmatch(_ENV_NAME_RE, s)]
        if bad:
            raise ValueError(
                f"secrets must be environment variable NAMES (UPPER_SNAKE_CASE), "
                f"not values: {bad}"
            )
        return v


class MigrationTarget(BaseModel):
    # Keep the planned values in the schema so an existing draft gets a useful
    # "not deployed" diagnostic instead of an opaque enum error. Only
    # AgentCore + adapter has infrastructure behind it today.
    runtime: Literal["agentcore", "ec2"] = "agentcore"
    mode: Literal["adapter", "native"] = "adapter"

    model_config = {"extra": "forbid"}

    @field_validator("runtime")
    @classmethod
    def _runtime_is_deployed(cls, v: str) -> str:
        if v != "agentcore":
            raise ValueError(
                "migration.target.runtime 'ec2' is not deployed by this "
                "accelerator; only 'agentcore' is currently supported"
            )
        return v

    @field_validator("mode")
    @classmethod
    def _mode_is_deployed(cls, v: str) -> str:
        if v != "adapter":
            raise ValueError(
                "migration.target.mode 'native' is not deployed by this "
                "accelerator; only 'adapter' is currently supported"
            )
        return v


class MigrationGate(BaseModel):
    """Customer evidence required before an external migration stage proceeds.

    A gate is deliberately evidence, not an executable switch. The
    accelerator may deploy and verify the target while a gate is incomplete,
    but ``migrate readiness`` reports the stage as BLOCKED. Adding
    ``approved_at`` is the final assertion and therefore requires the rest of
    the audit record to be complete.
    """

    owner: str = Field(default="", max_length=200)
    approver: str = Field(default="", max_length=200)
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    evidence: list[str] = Field(default_factory=list, max_length=50)
    rollback: str = Field(default="", max_length=2000)

    model_config = {"extra": "forbid"}

    @field_validator("owner", "approver", "rollback")
    @classmethod
    def _text_is_safe_to_render(cls, v: str) -> str:
        return _single_line_reference(v)

    @field_validator("evidence")
    @classmethod
    def _evidence_is_safe_to_render(cls, v: list[str]) -> list[str]:
        for item in v:
            if len(item) > 500:
                raise ValueError("evidence references must be at most 500 characters")
            _single_line_reference(item, "evidence reference")
        return v

    @model_validator(mode="after")
    def _approval_is_complete(self) -> MigrationGate:
        if (
            self.owner.strip()
            and self.approver.strip()
            and self.owner.strip().casefold() == self.approver.strip().casefold()
        ):
            raise ValueError(
                "owner and approver must be different for separation of duties"
            )
        if self.approved_at is None:
            if self.expires_at is not None:
                raise ValueError("expires_at requires approved_at")
            return self
        missing = self._missing_fields()
        if missing:
            raise ValueError(
                "approved_at requires a complete gate; missing " + ", ".join(missing)
            )
        if self.approved_at.tzinfo is None:
            raise ValueError(
                "approved_at must include a UTC offset, for example +00:00"
            )
        if self.approved_at > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("approved_at cannot be in the future")
        assert self.expires_at is not None
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a UTC offset, for example +00:00")
        if self.expires_at <= self.approved_at:
            raise ValueError("expires_at must be later than approved_at")
        if self.expires_at > self.approved_at + timedelta(days=7):
            raise ValueError("approval validity must not exceed 7 days")
        return self

    def _missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.owner.strip():
            missing.append("owner")
        if not self.approver.strip():
            missing.append("approver")
        if not self.evidence or not all(item.strip() for item in self.evidence):
            missing.append("evidence")
        if not self.rollback.strip():
            missing.append("rollback")
        if self.approved_at is None:
            missing.append("approved_at")
        if self.expires_at is None:
            missing.append("expires_at")
        return missing

    @property
    def blockers(self) -> list[str]:
        blockers = self._missing_fields()
        if self.expires_at is not None and self.expires_at <= datetime.now(UTC):
            blockers.append("approval expired")
        return blockers

    @property
    def ready(self) -> bool:
        return not self.blockers


class MigrationRetainedDataset(BaseModel):
    """One existing private datastore retained behind the migrated runtime."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    adapter: Literal["retain-source-v1"] = "retain-source-v1"
    dependency: str
    classification_reference: str = Field(min_length=1, max_length=500)
    retention_reference: str = Field(min_length=1, max_length=500)
    identity_mapping_reference: str = Field(min_length=1, max_length=500)
    validation_reference: str = Field(min_length=1, max_length=500)

    model_config = {"extra": "forbid"}

    @field_validator("dependency")
    @classmethod
    def _dependency_is_a_hostname(cls, v: str) -> str:
        if not _HOSTNAME_RE.fullmatch(v):
            raise ValueError(
                "dependency must be a hostname without scheme, port, or path"
            )
        return v

    @field_validator(
        "classification_reference",
        "retention_reference",
        "identity_mapping_reference",
        "validation_reference",
    )
    @classmethod
    def _references_are_single_line(cls, v: str) -> str:
        return _single_line_reference(v, "evidence reference")


class MigrationDataStage(BaseModel):
    """Optional customer-data handling; execution remains source-specific."""

    strategy: Literal["none", "retain-source", "external-copy"] = "none"
    datasets: list[MigrationRetainedDataset] = Field(
        default_factory=list, max_length=25
    )
    gate: MigrationGate = Field(default_factory=MigrationGate)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _strategy_has_the_right_dataset_contract(self) -> MigrationDataStage:
        if self.strategy == "retain-source" and not self.datasets:
            raise ValueError(
                "strategy 'retain-source' requires at least one dataset using "
                "adapter 'retain-source-v1'"
            )
        if self.strategy != "retain-source" and self.datasets:
            raise ValueError(
                "datasets are currently supported only with strategy "
                "'retain-source'; executable copy adapters are not installed"
            )
        names = [dataset.name for dataset in self.datasets]
        if len(names) != len(set(names)):
            raise ValueError("dataset names must be unique")
        return self


class MigrationRuntimeStage(BaseModel):
    """Immutable deployed target and its live verification receipt."""

    account_id: str = Field(default="", pattern=r"^(?:|\d{12})$")
    runtime_arn: str = Field(default="", max_length=500)
    source_hash: str = Field(default="", pattern=r"^(?:|[a-f0-9]{16})$")
    image_digest: str = Field(default="", pattern=r"^(?:|sha256:[a-f0-9]{64})$")
    verification_reference: str = Field(default="", max_length=500)
    gate: MigrationGate = Field(default_factory=MigrationGate)

    model_config = {"extra": "forbid"}

    @field_validator("account_id")
    @classmethod
    def _account_is_not_a_placeholder(cls, v: str) -> str:
        if v in _ACCOUNT_SENTINELS:
            raise ValueError("must be the real target AWS account, not a placeholder")
        return v

    @field_validator("runtime_arn", "verification_reference")
    @classmethod
    def _references_are_single_line(cls, v: str) -> str:
        return _single_line_reference(v)

    @field_validator("runtime_arn")
    @classmethod
    def _runtime_arn_is_agentcore(cls, v: str) -> str:
        if v and not re.fullmatch(
            r"arn:[^:]+:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/\S+",
            v,
        ):
            raise ValueError("must be an AgentCore Runtime ARN")
        return v

    @property
    def blockers(self) -> list[str]:
        return [
            name
            for name, value in (
                ("account_id", self.account_id),
                ("runtime_arn", self.runtime_arn),
                ("source_hash", self.source_hash),
                ("image_digest", self.image_digest),
                ("verification_reference", self.verification_reference),
            )
            if not value
        ]


class MigrationTriggerStage(BaseModel):
    """Optional shadowing of the source event trigger."""

    strategy: Literal["none", "external-shadow"] = "none"
    source_reference: str = Field(default="", max_length=500)
    shadow_reference: str = Field(default="", max_length=500)
    safety_mode: (
        Literal["read-only", "dry-run", "idempotent", "dual-publish"] | None
    ) = None
    idempotency_reference: str = Field(default="", max_length=500)
    signature_validation_reference: str = Field(default="", max_length=500)
    validation_reference: str = Field(default="", max_length=500)
    gate: MigrationGate = Field(default_factory=MigrationGate)

    model_config = {"extra": "forbid"}

    @field_validator(
        "source_reference",
        "shadow_reference",
        "idempotency_reference",
        "signature_validation_reference",
        "validation_reference",
    )
    @classmethod
    def _references_are_single_line(cls, v: str) -> str:
        return _single_line_reference(v)

    @model_validator(mode="after")
    def _shadow_plan_is_complete(self) -> MigrationTriggerStage:
        planned = any(
            (
                self.source_reference,
                self.shadow_reference,
                self.safety_mode,
                self.idempotency_reference,
                self.signature_validation_reference,
                self.validation_reference,
            )
        )
        if self.strategy == "none":
            if planned:
                raise ValueError(
                    "trigger plan fields require strategy 'external-shadow'"
                )
            return self
        missing = [
            name
            for name, value in (
                ("source_reference", self.source_reference),
                ("shadow_reference", self.shadow_reference),
                ("safety_mode", self.safety_mode),
                ("validation_reference", self.validation_reference),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "strategy 'external-shadow' requires " + ", ".join(missing)
            )
        if self.source_reference == self.shadow_reference:
            raise ValueError("source_reference and shadow_reference must be different")
        return self


class MigrationAbortThresholds(BaseModel):
    """Customer-approved stop conditions for a traffic step."""

    max_error_rate_percent: float = Field(ge=0, le=100)
    max_p95_latency_ms: int = Field(ge=1, le=3_600_000)
    max_failed_events: int = Field(ge=0)
    observation_minutes: int = Field(ge=1, le=1440)

    model_config = {"extra": "forbid"}

    @field_validator("max_error_rate_percent", mode="before")
    @classmethod
    def _error_rate_is_a_number_not_a_boolean(cls, v: object) -> object:
        if type(v) not in {int, float}:
            raise ValueError("must be a number, not a boolean or string")
        return v

    @field_validator(
        "max_p95_latency_ms",
        "max_failed_events",
        "observation_minutes",
        mode="before",
    )
    @classmethod
    def _counts_are_integers_not_booleans(cls, v: object) -> object:
        if type(v) is not int:
            raise ValueError("must be an integer, not a boolean or string")
        return v


class MigrationTrafficStage(BaseModel):
    """Optional traffic shift; the external router remains customer-owned."""

    strategy: Literal["none", "external-canary", "external-switch"] = "none"
    router_reference: str = Field(default="", max_length=500)
    source_reference: str = Field(default="", max_length=500)
    target_reference: str = Field(default="", max_length=500)
    metrics_reference: str = Field(default="", max_length=500)
    canary_steps_percent: list[int] = Field(default_factory=list, max_length=10)
    abort: MigrationAbortThresholds | None = None
    gate: MigrationGate = Field(default_factory=MigrationGate)

    model_config = {"extra": "forbid"}

    @field_validator(
        "router_reference",
        "source_reference",
        "target_reference",
        "metrics_reference",
    )
    @classmethod
    def _references_are_single_line(cls, v: str) -> str:
        return _single_line_reference(v)

    @field_validator("canary_steps_percent", mode="before")
    @classmethod
    def _steps_are_integers_not_booleans(cls, v: object) -> object:
        if not isinstance(v, list) or any(type(step) is not int for step in v):
            raise ValueError(
                "must be a list of integers, not booleans or numeric strings"
            )
        return v

    @model_validator(mode="after")
    def _traffic_plan_is_complete(self) -> MigrationTrafficStage:
        planned = any(
            (
                self.router_reference,
                self.source_reference,
                self.target_reference,
                self.metrics_reference,
                self.canary_steps_percent,
                self.abort,
            )
        )
        if self.strategy == "none":
            if planned:
                raise ValueError("traffic plan fields require a non-none strategy")
            return self
        missing = [
            name
            for name, value in (
                ("router_reference", self.router_reference),
                ("source_reference", self.source_reference),
                ("target_reference", self.target_reference),
                ("metrics_reference", self.metrics_reference),
                ("abort", self.abort),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"strategy {self.strategy!r} requires " + ", ".join(missing)
            )
        if self.source_reference == self.target_reference:
            raise ValueError("source_reference and target_reference must be different")
        if self.strategy == "external-canary":
            steps = self.canary_steps_percent
            if (
                not steps
                or any(step < 1 or step > 100 for step in steps)
                or steps != sorted(set(steps))
                or steps[-1] != 100
            ):
                raise ValueError(
                    "external-canary requires unique, increasing "
                    "canary_steps_percent between 1 and 100, ending at 100"
                )
        elif self.canary_steps_percent:
            raise ValueError(
                "external-switch is atomic and must not set canary_steps_percent"
            )
        return self


class MigrationStages(BaseModel):
    """Opt-in work around the runtime move.

    ``none`` is a real, visible scope decision. The currently supported
    non-none strategies are intentionally named ``external-*`` because this
    repository validates their evidence but does not mutate customer systems
    of record, event sources, or traffic routers.
    """

    runtime: MigrationRuntimeStage = Field(default_factory=MigrationRuntimeStage)
    data: MigrationDataStage = Field(default_factory=MigrationDataStage)
    triggers: MigrationTriggerStage = Field(default_factory=MigrationTriggerStage)
    traffic: MigrationTrafficStage = Field(default_factory=MigrationTrafficStage)

    model_config = {"extra": "forbid"}


class MigrationNetworkReceipt(BaseModel):
    """Deployed VPC placement and private-dependency probe evidence."""

    vpc_id: str = Field(default="", pattern=r"^(?:|vpc-[0-9a-f]{8,17})$")
    private_subnet_ids: list[str] = Field(default_factory=list, max_length=20)
    security_group_ids: list[str] = Field(default_factory=list, max_length=20)
    ca_bundle_version_id: str = Field(default="", max_length=256)
    probe_verification_reference: str = Field(default="", max_length=500)

    model_config = {"extra": "forbid"}

    @field_validator("private_subnet_ids")
    @classmethod
    def _subnets_are_unique_ids(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("private_subnet_ids must not contain duplicates")
        bad = [item for item in v if not re.fullmatch(r"subnet-[0-9a-f]{8,17}", item)]
        if bad:
            raise ValueError(f"private_subnet_ids contain invalid subnet IDs: {bad}")
        return v

    @field_validator("security_group_ids")
    @classmethod
    def _security_groups_are_unique_ids(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("security_group_ids must not contain duplicates")
        bad = [item for item in v if not re.fullmatch(r"sg-[0-9a-f]{8,17}", item)]
        if bad:
            raise ValueError(
                f"security_group_ids contain invalid security group IDs: {bad}"
            )
        return v

    @field_validator("ca_bundle_version_id", "probe_verification_reference")
    @classmethod
    def _references_are_single_line(cls, v: str) -> str:
        return _single_line_reference(v)

    @property
    def blockers(self) -> list[str]:
        return [
            name
            for name, value in (
                ("vpc_id", self.vpc_id),
                ("private_subnet_ids", self.private_subnet_ids),
                ("security_group_ids", self.security_group_ids),
                ("probe_verification_reference", self.probe_verification_reference),
            )
            if not value
        ]

    @property
    def populated(self) -> bool:
        return any(
            (
                self.vpc_id,
                self.private_subnet_ids,
                self.security_group_ids,
                self.ca_bundle_version_id,
                self.probe_verification_reference,
            )
        )


class MigrationNetwork(BaseModel):
    """What the migrated agent must still reach on the customer side."""

    private_dependencies: list[str] = Field(
        default_factory=list, max_length=25
    )  # hostnames
    connectivity: Literal["vpn", "transit-gateway", "none"] = "none"
    dns_forwarders: list[str] = Field(default_factory=list)  # IPv4 resolvers
    ca_bundle_secret_name: str = ""  # Secrets Manager NAME of a private CA bundle
    receipt: MigrationNetworkReceipt = Field(default_factory=MigrationNetworkReceipt)
    gate: MigrationGate = Field(default_factory=MigrationGate)

    model_config = {"extra": "forbid"}

    @field_validator("private_dependencies")
    @classmethod
    def _hostnames(cls, v: list[str]) -> list[str]:
        bad = [h for h in v if not _HOSTNAME_RE.match(h)]
        if bad:
            raise ValueError(
                f"private_dependencies must be hostnames (no scheme, port or "
                f"path): {bad}"
            )
        if len(set(v)) != len(v):
            raise ValueError("private_dependencies must not contain duplicates")
        if len(json.dumps(v, separators=(",", ":")).encode()) > 3000:
            raise ValueError(
                "private_dependencies exceed the bounded probe allow-list size; "
                "use fewer or shorter hostnames (maximum encoded size: 3000 bytes)"
            )
        return v

    @field_validator("dns_forwarders")
    @classmethod
    def _ipv4(cls, v: list[str]) -> list[str]:
        for ip in v:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError as exc:
                raise ValueError(
                    f"dns_forwarders must be IPv4 addresses: {ip!r}"
                ) from exc
        return v

    @field_validator("ca_bundle_secret_name")
    @classmethod
    def _ca_secret_is_a_name(cls, v: str) -> str:
        if v and (
            not _SECRET_NAME_RE.fullmatch(v) or _JWT_RE.match(v) or _BLOB_RE.match(v)
        ):
            raise ValueError(
                "ca_bundle_secret_name must be a Secrets Manager NAME, not a "
                "secret value"
            )
        return v

    @model_validator(mode="after")
    def _ca_receipt_matches_plan(self) -> MigrationNetwork:
        if self.receipt.ca_bundle_version_id and not self.ca_bundle_secret_name:
            raise ValueError(
                "receipt.ca_bundle_version_id requires ca_bundle_secret_name"
            )
        return self


class MigrationConfig(BaseModel):
    """Migrate an existing agent onto the platform.

    Stack names do NOT change: the migrated agent deploys as
    `<prefix>-runtime-orchestrator` on both targets, so verify, dashboard and
    destroy keep working with zero contract churn (owner decision, final).
    When this block is present `agents.pattern` no longer selects the runtime
    image — the source does — but it is still emitted as AGENT_PATTERN so
    existing consumers keep working.

    Cross-field rules that touch `security` live on PlatformConfig; the
    non-fatal ones surface as `warnings`.
    """

    source: MigrationSource
    target: MigrationTarget = Field(default_factory=MigrationTarget)
    network: MigrationNetwork = Field(default_factory=MigrationNetwork)
    stages: MigrationStages = Field(default_factory=MigrationStages)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _source_and_mode(self) -> MigrationConfig:
        src, mode = self.source, self.target.mode
        if bool(src.image) == bool(src.build):
            raise ValueError(
                "migration.source needs exactly one of image (pre-built reference) "
                "or build (context + dockerfile)"
            )
        if mode == "adapter" and not (src.port and src.invoke_path):
            raise ValueError(
                "target.mode 'adapter' requires migration.source.port and "
                "migration.source.invoke_path (where the adapter forwards "
                "/invocations to)"
            )
        if mode == "native" and (src.port or src.invoke_path):
            raise ValueError(
                "target.mode 'native' means the image already speaks the AgentCore "
                "contract (8080, /invocations, /ping) — drop migration.source.port "
                "and migration.source.invoke_path, or use mode 'adapter'"
            )
        if (
            self.stages.traffic.strategy != "none"
            and src.trigger in {"webhook", "schedule", "queue"}
            and self.stages.triggers.strategy == "none"
        ):
            raise ValueError(
                f"migration.stages.traffic for source trigger {src.trigger!r} "
                "requires migration.stages.triggers.strategy='external-shadow'; "
                "shift the event source through a disabled/shadow target before "
                "moving traffic"
            )
        trigger_stage = self.stages.triggers
        if trigger_stage.strategy != "none":
            allowed_safety = {
                "http": {"read-only", "idempotent"},
                "webhook": {"read-only", "idempotent"},
                "schedule": {"dry-run", "idempotent"},
                "queue": {"dual-publish"},
            }[src.trigger]
            if trigger_stage.safety_mode not in allowed_safety:
                raise ValueError(
                    f"source trigger {src.trigger!r} requires safety_mode in "
                    f"{sorted(allowed_safety)}, got {trigger_stage.safety_mode!r}"
                )
            if (
                trigger_stage.safety_mode in {"idempotent", "dual-publish"}
                and not trigger_stage.idempotency_reference
            ):
                raise ValueError(
                    f"safety_mode {trigger_stage.safety_mode!r} requires "
                    "idempotency_reference"
                )
            if src.trigger == "webhook" and not (
                trigger_stage.signature_validation_reference
            ):
                raise ValueError(
                    "webhook shadowing requires signature_validation_reference; "
                    "a generic unauthenticated webhook is not supported"
                )
        traffic_stage = self.stages.traffic
        if traffic_stage.strategy == "external-canary" and src.trigger in {
            "schedule",
            "queue",
        }:
            raise ValueError(
                f"source trigger {src.trigger!r} cannot use a percentage canary; "
                "use strategy 'external-switch' after a safe shadow rehearsal"
            )
        retained_dependencies = {
            dataset.dependency for dataset in self.stages.data.datasets
        }
        undeclared = sorted(
            retained_dependencies - set(self.network.private_dependencies)
        )
        if undeclared:
            raise ValueError(
                "retain-source datasets must name dependencies from "
                f"migration.network.private_dependencies; missing {undeclared}"
            )
        return self

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.source.build and self.source.build._compatibility_warning:
            out.append(self.source.build._compatibility_warning)
        if self.target.runtime == "agentcore" and self.source.image:
            out.append(
                "AgentCore Runtime is arm64-only and a pre-built image "
                f"({self.source.image}) cannot be verified for it before deploy. "
                "Prefer migration.source.build, which is built arm64 here. "
                "If using a pre-built image, pin image@sha256:<digest>; "
                "same-tag repushes are not detected."
            )
        net = self.network
        if not net.private_dependencies and (
            net.dns_forwarders or net.ca_bundle_secret_name or net.receipt.populated
        ):
            out.append(
                "migration.network DNS/CA/receipt fields are set but "
                "private_dependencies is empty — nothing will use them."
            )
        for name, stage in (
            ("data", self.stages.data),
            ("triggers", self.stages.triggers),
            ("traffic", self.stages.traffic),
        ):
            gate = stage.gate
            if stage.strategy == "none" and any(
                (
                    gate.owner,
                    gate.approver,
                    gate.approved_at,
                    gate.expires_at,
                    gate.evidence,
                    gate.rollback,
                )
            ):
                out.append(
                    f"migration.stages.{name}.gate is populated but strategy is "
                    "'none'; the stage remains out of scope"
                )
        if not net.private_dependencies and any(
            (
                net.gate.owner,
                net.gate.approver,
                net.gate.approved_at,
                net.gate.expires_at,
                net.gate.evidence,
                net.gate.rollback,
            )
        ):
            out.append(
                "migration.network.gate is populated but private_dependencies is "
                "empty; connectivity remains out of scope"
            )
        return out


# ── Use cases ──
# A use case is a self-contained product integration under use-cases/<name>/:
# a manifest (this model), a CDK stack, a verify script, and a walkthrough.
# It consumes the platform through its published interface (the SSM parameter
# namespace + Cognito tokens — docs/PLATFORM_INTERFACE.md), never through core
# stack internals, so contributors and the core can evolve independently.
# Nothing deploys unless platform.yaml names the use case under `use_cases:`.
USE_CASES_DIR = Path(__file__).resolve().parents[1] / "use-cases"


class UseCaseManifest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,40}$")
    owner: str  # alias or team — who reviews changes to this folder
    summary: str
    # Core stack suffixes this use case needs in the footprint (e.g. gateway).
    requires: list[str] = Field(default_factory=list)
    # Stack suffixes this use case adds. Namespaced so a contribution can
    # never collide with (or masquerade as) a core stack.
    stacks: list[str] = Field(min_length=1)
    entry: str = "stack.py"  # module exposing build(app, platform_ctx, config)

    model_config = {"extra": "forbid"}

    @field_validator("stacks")
    @classmethod
    def _stacks_are_namespaced(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if not s.startswith("uc-")]
        if bad:
            raise ValueError(f"use-case stack suffixes must start with 'uc-': {bad}")
        return v


def discover_use_cases(root: Path = USE_CASES_DIR) -> dict[str, UseCaseManifest]:
    """Load every use-cases/<name>/manifest.yaml. A broken manifest is a hard
    error naming the file — a contribution must not half-load."""
    found: dict[str, UseCaseManifest] = {}
    if not root.is_dir():
        return found
    for mf in sorted(root.glob("*/manifest.yaml")):
        # use-cases/_template/ is what `deploy.sh usecase new` copies from; its
        # manifest holds {{tokens}} and is not a use case.
        if mf.parent.name.startswith("_"):
            continue
        manifest = UseCaseManifest.model_validate(yaml.safe_load(mf.read_text()))
        if manifest.name != mf.parent.name:
            raise ValueError(
                f"{mf}: manifest name {manifest.name!r} must match its "
                f"directory {mf.parent.name!r}"
            )
        found[manifest.name] = manifest
    return found


class PlatformConfig(BaseModel):
    """The root model — `platform.yaml` parses into exactly this."""

    project: str = Field(
        default="agentcore-workshop", pattern=r"^[a-z][a-z0-9-]{2,32}$"
    )
    environment: str = Field(default="dev", pattern=r"^[a-z][a-z0-9]{1,15}$")
    region: str = Field(default="us-east-1", pattern=_REGION_RE)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    # Opt-in use cases: name → that use case's own config block (passed to its
    # build() untouched; {} to enable with defaults). Names must exist under
    # use-cases/ — validated below so a typo cannot silently deploy nothing.
    use_cases: dict[str, dict] = Field(default_factory=dict)
    # Absent = no migration; everything behaves exactly as before.
    migration: MigrationConfig | None = None

    model_config = {"extra": "forbid"}  # a typo'd key is an error, not a no-op

    @model_validator(mode="after")
    def _derived_names_fit_service_limits(self) -> PlatformConfig:
        # The longest fixed suffixes on generated resource names leave 30
        # characters for "<project>-<environment>". Memory strategies allow
        # 48 total and Logs delivery names allow 60. Catch this during Design;
        # CloudFormation otherwise emits warnings and fails minutes later.
        prefix = f"{self.project}-{self.environment}"
        if len(prefix) > 30:
            raise ValueError(
                f"project/environment prefix {prefix!r} is {len(prefix)} "
                "characters; use at most 30 so generated AgentCore Memory and "
                "CloudWatch Logs delivery names fit their service limits"
            )
        return self

    @field_validator("use_cases")
    @classmethod
    def _use_cases_exist(cls, v: dict[str, dict]) -> dict[str, dict]:
        if not v:
            return v
        available = discover_use_cases()
        unknown = sorted(set(v) - set(available))
        if unknown:
            raise ValueError(
                f"unknown use case(s) {unknown}; available: {sorted(available)} "
                "(a use case is a directory under use-cases/ with a manifest.yaml)"
            )
        return v

    @model_validator(mode="after")
    def _migration_refuses_production(self) -> PlatformConfig:
        # Same rule app.py and preflight apply, enforced at validation so
        # `python -m infra_utils.platform_config` and synth agree.
        error = migration_production_error(
            self.deployment.mode, self.migration is not None
        )
        if error:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _guardrails_need_an_attachable_model(self) -> PlatformConfig:
        # The Claude Agent SDK's Bedrock path has no way to attach a Bedrock
        # Guardrail, so the require_guardrails IAM deny would block every
        # inference from those patterns — a bricked agent, not a hardened one.
        if self.security.require_guardrails and self.agents.pattern.startswith(
            "claude-sdk"
        ):
            raise ValueError(
                f"security.require_guardrails is incompatible with agents.pattern "
                f"{self.agents.pattern!r}: the Claude Agent SDK cannot attach a "
                "Bedrock Guardrail to its inference calls, so the IAM deny would "
                "block every inference from this pattern"
            )
        return self

    @model_validator(mode="after")
    def _migration_needs_the_vpc(self) -> PlatformConfig:
        # Validation rather than a new expected_stacks() branch: networking is
        # already in the footprint whenever security.networking is true, so
        # requiring the flag keeps the contract to one rule per stack.
        m = self.migration
        if m is None:
            return self
        if m.network.private_dependencies:
            if not self.security.networking:
                raise ValueError(
                    "migration.network.private_dependencies needs the agent in the "
                    "VPC: set security.networking: true"
                )
            if m.network.connectivity == "none":
                raise ValueError(
                    "migration.network.private_dependencies needs a path to the "
                    "customer network: set migration.network.connectivity to "
                    "'vpn' or 'transit-gateway'"
                )
        return self

    @model_validator(mode="after")
    def _migration_runtime_receipt_matches_target(self) -> PlatformConfig:
        m = self.migration
        if m is None:
            return self
        runtime = m.stages.runtime
        if runtime.runtime_arn:
            arn = runtime.runtime_arn.split(":", 5)
            arn_region, arn_account = arn[3], arn[4]
            if arn_region != self.region:
                raise ValueError(
                    "migration.stages.runtime.runtime_arn must use platform "
                    f"region {self.region!r}, got {arn_region!r}"
                )
            if runtime.account_id and arn_account != runtime.account_id:
                raise ValueError(
                    "migration.stages.runtime.runtime_arn account must match "
                    "migration.stages.runtime.account_id"
                )
        account = runtime.account_id
        if not account:
            return self
        if self.deployment.strategy == "federated":
            if account not in self.deployment.workload_accounts:
                raise ValueError(
                    "migration.stages.runtime.account_id must be one of "
                    "deployment.workload_accounts for a federated migration"
                )
        elif (
            self.deployment.platform_account
            and account != self.deployment.platform_account
        ):
            raise ValueError(
                "migration.stages.runtime.account_id must match "
                "deployment.platform_account"
            )
        return self

    @model_validator(mode="after")
    def _production_controls_are_complete(self) -> PlatformConfig:
        if self.deployment.mode != "production":
            return self

        missing: list[str] = []

        def require(condition: bool, field: str, expectation: str) -> None:
            if not condition:
                missing.append(f"{field}: {expectation}")

        require(
            self.identity.idp != "cognito",
            "identity.idp",
            "use an enterprise IdP (entra_id, okta, or ping)",
        )
        require(
            bool(self.deployment.platform_account),
            "deployment.platform_account",
            "provide the 12-digit AWS account this deployment may modify",
        )
        require(self.security.networking, "security.networking", "set true")
        require(
            self.security.cloudtrail_alerting,
            "security.cloudtrail_alerting",
            "set true",
        )
        require(
            self.security.resource_policies,
            "security.resource_policies",
            "set true",
        )
        require(self.security.egress_filter, "security.egress_filter", "set true")
        require(
            self.security.require_guardrails,
            "security.require_guardrails",
            "set true",
        )
        require(self.security.cedar.enabled, "security.cedar.enabled", "set true")
        require(
            self.security.cedar.mode == "ENFORCE",
            "security.cedar.mode",
            "set ENFORCE",
        )
        require(self.security.traceability, "security.traceability", "set true")
        require(bool(self.security.org_id), "security.org_id", "provide the AWS Org id")
        require(bool(self.agents.model_id), "agents.model_id", "select a model")
        require(
            bool(self.agents.allowed_models),
            "agents.allowed_models",
            "provide a non-empty model allow-list",
        )
        require(
            self.gateway.web_search != "auto",
            "gateway.web_search",
            "choose on or off explicitly",
        )
        require(
            self.observability.transaction_search,
            "observability.transaction_search",
            "set true",
        )
        require(self.observability.alarms, "observability.alarms", "set true")
        require(
            bool(self.observability.alarm_email),
            "observability.alarm_email",
            "provide a monitored inbox",
        )
        if missing:
            raise ValueError(
                "deployment.mode 'production' requires all production controls:\n- "
                + "\n- ".join(missing)
            )
        return self

    @model_validator(mode="after")
    def _account_scoped_idp_secret_has_an_owner(self) -> PlatformConfig:
        """Bind centralized/distributed IdP secrets to an intended account.

        A Secrets Manager name is only meaningful inside one account and
        Region. Without this binding, valid credentials for another account
        turn a missing-secret lookup into a prompt and can deploy an
        identically named platform into the wrong place.
        """
        if (
            self.identity.idp != "cognito"
            and self.deployment.strategy != "federated"
            and not self.deployment.platform_account
        ):
            raise ValueError(
                "identity.client_secret_name is account-scoped: set "
                "deployment.platform_account to the 12-digit AWS account this "
                "deployment may modify"
            )
        return self

    @property
    def warnings(self) -> list[str]:
        """Everything non-fatal about this file: placeholders that validation
        let through because PLATFORM_ALLOW_PLACEHOLDERS=1 (otherwise they are
        errors and the model never exists), plus the migration warnings."""
        out = (
            identity_placeholders(self.identity)
            + deployment_placeholders(self.deployment)
            + security_org_placeholders(self.security.org_id)
            + observability_placeholders(self.observability.alarm_email)
        )
        # The migration block (PR #63) carries its own warnings; getattr keeps
        # this property valid on a tree where that field does not exist yet.
        migration = getattr(self, "migration", None)
        return out + (migration.warnings if migration else [])

    @property
    def web_search_enabled(self) -> bool:
        """Resolve gateway.web_search 'auto' against the launch regions."""
        if self.gateway.web_search == "auto":
            return self.region in WEB_SEARCH_REGIONS
        return self.gateway.web_search == "on"

    def expected_stacks(self, account: str = "") -> list[str]:
        """The exact stack names this config makes app.py synthesize.

        This is the deployment contract: deploy plans, verification, the
        dashboard, and destroy all consume this list instead of growing their
        own opinion of the footprint. It deliberately duplicates app.py's
        stack-existence logic — scripts/check-contract.sh synthesizes every
        preset and fails CI when the two drift, which is what keeps the
        duplication honest. If you add a stack to app.py, add it here and the
        parity check goes green again.

        `account` matters only for strategy 'federated', where the account
        deployed into decides the role (platform: shared services, no agent
        runtimes; workload: runtimes + own memory/identity, no auth/gateway).
        """
        role = self.federated_role(account)
        prefix = f"{self.project}-{self.environment}"
        stacks: list[str] = []
        # The VPC only hosts agent runtimes: none on a federated platform side.
        if self.security.networking and role != "platform":
            stacks.append(f"{prefix}-networking")
        if self.security.cloudtrail_alerting:
            stacks.append(f"{prefix}-security")
        if role != "workload":
            stacks.append(f"{prefix}-auth")
        stacks.append(f"{prefix}-identity")
        if role != "platform":
            stacks.append(f"{prefix}-memory")
        if role != "workload":
            stacks.append(f"{prefix}-gateway")
        if role != "platform":
            stacks.append(f"{prefix}-runtime-orchestrator")
            if self.agents.a2a:
                stacks.append(f"{prefix}-runtime-code-agent")
                stacks.append(f"{prefix}-runtime-research-agent")
        stacks.append(f"{prefix}-observability")

        # Enabled use cases ride at the end. Their `requires` are core stack
        # suffixes that must be in THIS footprint — checked here because the
        # footprint is role-dependent (a federated workload account has no
        # local gateway, so a gateway-requiring use case belongs platform-side).
        if self.use_cases:
            manifests = discover_use_cases()
            suffixes = {s.removeprefix(f"{prefix}-") for s in stacks}
            for name in sorted(self.use_cases):
                missing = sorted(set(manifests[name].requires) - suffixes)
                if missing:
                    raise ValueError(
                        f"use case {name!r} requires {missing}, absent from this "
                        f"footprint (strategy={self.deployment.strategy}"
                        f"{', role=' + role if role else ''}). Enable the "
                        "prerequisites, or deploy the use case on the side of "
                        "the federation that runs them."
                    )
                stacks.extend(f"{prefix}-{s}" for s in manifests[name].stacks)
        return stacks

    def federated_role(self, account: str) -> str | None:
        """Which side of a federated deployment this account is.

        The same platform.yaml works in both accounts — the account you
        deploy into decides what gets deployed. Deploying a federated file
        from an account named in neither list is a hard error: it is the
        config-file version of deploying to the wrong account.
        """
        if self.deployment.strategy != "federated":
            return None
        if account == self.deployment.platform_account:
            return "platform"
        if account in self.deployment.workload_accounts:
            return "workload"
        raise ValueError(
            f"strategy is 'federated' but account {account or '(unset)'} is neither "
            f"deployment.platform_account ({self.deployment.platform_account}) nor in "
            f"deployment.workload_accounts ({self.deployment.workload_accounts})"
        )


def load_platform_config(path: str | Path) -> PlatformConfig:
    """Parse and validate a platform.yaml. Raises pydantic.ValidationError
    with EVERY problem in the file, not just the first."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return PlatformConfig.model_validate(raw)


def apply_environment_overrides(
    config: PlatformConfig, environ: dict[str, str] | None = None
) -> PlatformConfig:
    """Return the effective config after deploy.sh's supported env overrides.

    The Design phase used to print the manifest unchanged even when the same
    command would pass explicit environment values to CDK. That made the
    read-back actively misleading for identity changes. Keep this conversion
    close to ``to_env`` so the human plan and deployment share one vocabulary.
    Empty values are ignored, matching deploy.sh's fill-if-unset behavior.
    """
    env = environ if environ is not None else dict(os.environ)
    raw = config.model_dump()

    def put(path: tuple[str, ...], value: object) -> None:
        target = raw
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    scalar_paths = {
        "PROJECT_NAME": ("project",),
        "ENVIRONMENT": ("environment",),
        "AWS_REGION": ("region",),
        "DEPLOYMENT_MODE": ("deployment", "mode"),
        "DEPLOYMENT_STRATEGY": ("deployment", "strategy"),
        "PLATFORM_ACCOUNT": ("deployment", "platform_account"),
        "IDP_TYPE": ("identity", "idp"),
        "IDP_MODE": ("identity", "mode"),
        "IDP_TENANT_ID": ("identity", "tenant_id"),
        "IDP_CLIENT_ID": ("identity", "client_id"),
        "IDP_ISSUER_URL": ("identity", "issuer_url"),
        "IDP_CLIENT_SECRET_NAME": ("identity", "client_secret_name"),
        "AGENT_PATTERN": ("agents", "pattern"),
        "MODEL_ID": ("agents", "model_id"),
        "CEDAR_MODE": ("security", "cedar", "mode"),
        "ORG_ID": ("security", "org_id"),
        "ALARM_EMAIL": ("observability", "alarm_email"),
    }
    for key, path in scalar_paths.items():
        if env.get(key):
            put(path, env[key])

    bool_paths = {
        "ENABLE_A2A": ("agents", "a2a"),
        "USE_LONG_TERM_MEMORY": ("agents", "memory", "long_term"),
        "ENABLE_NETWORKING": ("security", "networking"),
        "ENABLE_SECURITY": ("security", "cloudtrail_alerting"),
        "ENABLE_RESOURCE_POLICIES": ("security", "resource_policies"),
        "ENABLE_EGRESS_FILTER": ("security", "egress_filter"),
        "REQUIRE_GUARDRAILS": ("security", "require_guardrails"),
        "ENABLE_CEDAR": ("security", "cedar", "enabled"),
        "ENABLE_TRACEABILITY": ("security", "traceability"),
        "ENABLE_TRANSACTION_SEARCH": ("observability", "transaction_search"),
        "ENABLE_ALARMS": ("observability", "alarms"),
    }
    for key, path in bool_paths.items():
        if not env.get(key):
            continue
        normalized = env[key].lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false, got {env[key]!r}")
        put(path, normalized == "true")

    if env.get("ENABLE_WEB_SEARCH"):
        normalized = env["ENABLE_WEB_SEARCH"].lower()
        if normalized not in {"true", "false"}:
            raise ValueError(
                "ENABLE_WEB_SEARCH must be true or false, "
                f"got {env['ENABLE_WEB_SEARCH']!r}"
            )
        put(("gateway", "web_search"), "on" if normalized == "true" else "off")
    if env.get("ALLOWED_MODELS"):
        put(
            ("agents", "allowed_models"),
            [item.strip() for item in env["ALLOWED_MODELS"].split(",") if item.strip()],
        )
    for key, path in {
        "ORCHESTRATOR_RUNTIME_GENERATION": (
            "agents",
            "orchestrator_runtime_generation",
        ),
        "LTM_TOP_K": ("agents", "memory", "top_k"),
        "LTM_RELEVANCE_SCORE": ("agents", "memory", "relevance_score"),
        "MEMORY_EVENT_EXPIRY_DAYS": ("agents", "memory", "event_expiry_days"),
        "LOG_RETENTION_DAYS": ("observability", "log_retention_days"),
    }.items():
        if env.get(key):
            put(path, env[key])

    return PlatformConfig.model_validate(raw)


def allowed_model_resources(models: list[str]) -> list[str]:
    """IAM resource ARNs for the runtime role's BedrockModels statement.

    A geo-prefixed entry (us., eu., ...) is a cross-region inference-profile
    id and emits BOTH the profile ARN and the base foundation-model ARN:
    invoking through a profile also requires foundation-model permissions in
    the profile's target regions — profile-only scoping breaks invokes.
    Foundation-model ARNs get a trailing '*' because Bedrock model ids carry
    version/context-window suffixes ('-v1:0', ':0:200k') that configs
    commonly omit.

    A `global.` profile is authorized against three resources (AWS docs,
    "Global cross-Region inference" IAM policy example:
    https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html):
    the profile ARN, the Regional foundation model, and the foundation model
    with an empty Region (global routing). The geo-prefix match below only
    covers 2-4 letter prefixes, so `global.` is handled first.
    """
    resources: list[str] = []
    for m in models:
        if m.startswith("global."):
            base = m[len("global.") :]
            resources += [
                f"arn:aws:bedrock:*:*:inference-profile/{m}",
                f"arn:aws:bedrock:*::foundation-model/{base}*",
                f"arn:aws:bedrock:::foundation-model/{base}*",
            ]
            continue
        prefix_match = re.match(r"^[a-z]{2,4}\.", m)
        base = m[prefix_match.end() :] if prefix_match else m
        if prefix_match and "." in base:
            resources.append(f"arn:aws:bedrock:*:*:inference-profile/{m}")
        else:
            base = m
        resources.append(f"arn:aws:bedrock:*::foundation-model/{base}*")
    return list(dict.fromkeys(resources))  # dedupe, order preserved


def _base_env(config: PlatformConfig) -> dict[str, str]:
    """Map the schema onto the env-var names deploy.sh and app.py already use.

    Only these names cross the shell boundary; everything else stays in the
    typed model. Empty values are omitted so fill-if-unset logic in deploy.sh
    never clobbers a variable with "".
    """
    pairs = {
        "PROJECT_NAME": config.project,
        "ENVIRONMENT": config.environment,
        "AWS_REGION": config.region,
        "DEPLOYMENT_MODE": config.deployment.mode,
        "DEPLOYMENT_STRATEGY": config.deployment.strategy,
        "PLATFORM_ACCOUNT": config.deployment.platform_account,
        "IDP_TYPE": config.identity.idp,
        "IDP_MODE": config.identity.mode,
        "IDP_TENANT_ID": config.identity.tenant_id,
        "IDP_CLIENT_ID": config.identity.client_id,
        "IDP_ISSUER_URL": config.identity.issuer_url,
        "IDP_CLIENT_SECRET_NAME": config.identity.client_secret_name,
        "AGENT_PATTERN": config.agents.pattern,
        "ORCHESTRATOR_RUNTIME_GENERATION": str(
            config.agents.orchestrator_runtime_generation
        ),
        "MODEL_ID": config.agents.model_id,
        "ALLOWED_MODELS": ",".join(config.agents.allowed_models),
        "ENABLE_A2A": str(config.agents.a2a).lower(),
        "USE_LONG_TERM_MEMORY": str(config.agents.memory.long_term).lower(),
        "MEMORY_EVENT_EXPIRY_DAYS": str(config.agents.memory.event_expiry_days),
        "LTM_TOP_K": str(config.agents.memory.top_k),
        "LTM_RELEVANCE_SCORE": str(config.agents.memory.relevance_score),
        "ENABLE_WEB_SEARCH": str(config.web_search_enabled).lower(),
        "ENABLE_NETWORKING": str(config.security.networking).lower(),
        "ENABLE_SECURITY": str(config.security.cloudtrail_alerting).lower(),
        "ENABLE_RESOURCE_POLICIES": str(config.security.resource_policies).lower(),
        "ENABLE_EGRESS_FILTER": str(config.security.egress_filter).lower(),
        "REQUIRE_GUARDRAILS": str(config.security.require_guardrails).lower(),
        "ENABLE_CEDAR": str(config.security.cedar.enabled).lower(),
        "CEDAR_MODE": config.security.cedar.mode,
        "ENABLE_TRACEABILITY": str(config.security.traceability).lower(),
        "ORG_ID": config.security.org_id,
        "ENABLE_TRANSACTION_SEARCH": str(
            config.observability.transaction_search
        ).lower(),
        "ENABLE_ALARMS": str(config.observability.alarms).lower(),
        "ALARM_EMAIL": config.observability.alarm_email,
        "LOG_RETENTION_DAYS": str(config.observability.log_retention_days),
        "MIGRATION_ENABLED": str(config.migration is not None).lower(),
    }
    if config.migration:
        m = config.migration
        build = m.source.build
        pairs.update(
            {
                "MIGRATION_SOURCE_PLATFORM": m.source.platform,
                "MIGRATION_SOURCE_IMAGE": m.source.image,
                "MIGRATION_BUILD_CONTEXT": build.context if build else "",
                "MIGRATION_BUILD_DOCKERFILE": build.dockerfile if build else "",
                "MIGRATION_REGISTRY_SECRET_NAME": m.source.registry_secret_name,
                "MIGRATION_PORT": str(m.source.port or ""),
                "MIGRATION_INVOKE_PATH": m.source.invoke_path,
                "MIGRATION_HEALTH_PATH": m.source.health_path,
                "MIGRATION_TRIGGER": m.source.trigger,
                "MIGRATION_ENV_JSON": json.dumps(
                    m.source.env, separators=(",", ":"), sort_keys=True
                ),
                # Compatibility for app.py/runtime_stack.py until they consume
                # MIGRATION_ENV_JSON. New consumers must use the JSON transport:
                # the legacy comma format cannot represent every valid value.
                "MIGRATION_ENV": ",".join(f"{k}={v}" for k, v in m.source.env.items()),
                "MIGRATION_SECRETS": ",".join(m.source.secrets),
                "MIGRATION_TARGET_RUNTIME": m.target.runtime,
                "MIGRATION_TARGET_MODE": m.target.mode,
                "MIGRATION_PRIVATE_DEPENDENCIES": ",".join(
                    m.network.private_dependencies
                ),
                "MIGRATION_CONNECTIVITY": m.network.connectivity,
                "MIGRATION_DNS_FORWARDERS": ",".join(m.network.dns_forwarders),
                "MIGRATION_CA_BUNDLE_SECRET_NAME": m.network.ca_bundle_secret_name,
            }
        )
    return {k: v for k, v in pairs.items() if v != ""}


def migration_secret_name(config: PlatformConfig, env_name: str) -> str:
    """Secrets Manager name for one migration.source.secrets entry."""
    return f"{config.project}/{config.environment}/migration/{env_name}"


@dataclass(frozen=True)
class MigrationReadinessCheck:
    """One cutover prerequisite and the evidence still missing for it."""

    name: str
    status: Literal["READY", "BLOCKED", "NOT REQUIRED"]
    detail: str


def _plan_digest(document: dict) -> str:
    canonical = json.dumps(
        document,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def migration_data_plan_document(config: PlatformConfig) -> dict:
    """Canonical, secret-free data plan whose digest can be approved."""
    m = config.migration
    if m is None:
        return {}
    document = {
        "version": "migration-data-plan/v1",
        "project": config.project,
        "environment": config.environment,
        "region": config.region,
        "target": {
            "runtime": m.target.runtime,
            "mode": m.target.mode,
        },
        "strategy": m.stages.data.strategy,
        "datasets": [
            dataset.model_dump(mode="json")
            for dataset in sorted(m.stages.data.datasets, key=lambda item: item.name)
        ],
    }
    return {
        **document,
        "digest": _plan_digest(document),
    }


def migration_runtime_plan_document(config: PlatformConfig) -> dict:
    """Canonical deployed-runtime receipt whose digest can be approved."""
    m = config.migration
    if m is None:
        return {}
    source = m.source.model_dump(mode="json")
    source["secrets"] = sorted(source["secrets"])
    document = {
        "version": "migration-runtime-plan/v1",
        "project": config.project,
        "environment": config.environment,
        "region": config.region,
        "deployment": {
            "mode": config.deployment.mode,
            "strategy": config.deployment.strategy,
            "platformAccount": config.deployment.platform_account,
            "workloadAccounts": sorted(config.deployment.workload_accounts),
        },
        "source": source,
        "target": {
            **m.target.model_dump(mode="json"),
            "runtimeGeneration": config.agents.orchestrator_runtime_generation,
        },
        "effectiveConfiguration": {
            "identity": config.identity.model_dump(mode="json"),
            "agents": config.agents.model_dump(mode="json"),
            "security": config.security.model_dump(mode="json"),
        },
        "receipt": m.stages.runtime.model_dump(mode="json", exclude={"gate"}),
    }
    return {**document, "digest": _plan_digest(document)}


def migration_network_plan_document(config: PlatformConfig) -> dict:
    """Canonical private-connectivity plan bound to the runtime account."""
    m = config.migration
    if m is None:
        return {}
    document = {
        "version": "migration-network-plan/v1",
        "project": config.project,
        "environment": config.environment,
        "region": config.region,
        "runtimeAccount": m.stages.runtime.account_id,
        "plan": {
            "privateDependencies": sorted(m.network.private_dependencies),
            "connectivity": m.network.connectivity,
            "dnsForwarders": sorted(m.network.dns_forwarders),
            "caBundleSecretName": m.network.ca_bundle_secret_name,
            "receipt": {
                "vpcId": m.network.receipt.vpc_id,
                "privateSubnetIds": sorted(m.network.receipt.private_subnet_ids),
                "securityGroupIds": sorted(m.network.receipt.security_group_ids),
                "caBundleVersionId": m.network.receipt.ca_bundle_version_id,
                "probeVerificationReference": (
                    m.network.receipt.probe_verification_reference
                ),
            },
        },
    }
    return {**document, "digest": _plan_digest(document)}


def migration_trigger_plan_document(config: PlatformConfig) -> dict:
    """Canonical event-source shadow plan, excluding its approval gate."""
    m = config.migration
    if m is None:
        return {}
    document = {
        "version": "migration-trigger-plan/v1",
        "project": config.project,
        "environment": config.environment,
        "region": config.region,
        "sourceTrigger": m.source.trigger,
        "plan": m.stages.triggers.model_dump(mode="json", exclude={"gate"}),
    }
    return {**document, "digest": _plan_digest(document)}


def migration_traffic_plan_document(config: PlatformConfig) -> dict:
    """Canonical traffic plan bound to data, trigger, and network decisions."""
    m = config.migration
    if m is None:
        return {}
    document = {
        "version": "migration-traffic-plan/v1",
        "project": config.project,
        "environment": config.environment,
        "region": config.region,
        "sourceTrigger": m.source.trigger,
        "target": {
            "runtime": m.target.runtime,
            "mode": m.target.mode,
        },
        "runtimePlanDigest": migration_runtime_plan_document(config)["digest"],
        "networkPlanDigest": (
            migration_network_plan_document(config)["digest"]
            if m.network.private_dependencies
            else ""
        ),
        "dataPlanDigest": (
            migration_data_plan_document(config)["digest"]
            if m.stages.data.strategy != "none"
            else ""
        ),
        "triggerPlanDigest": (
            migration_trigger_plan_document(config)["digest"]
            if m.stages.triggers.strategy != "none"
            else ""
        ),
        "operationalConfiguration": {
            "gateway": config.gateway.model_dump(mode="json"),
            "observability": config.observability.model_dump(mode="json"),
        },
        "plan": m.stages.traffic.model_dump(mode="json", exclude={"gate"}),
    }
    return {**document, "digest": _plan_digest(document)}


def _runtime_stage_check(config: PlatformConfig) -> MigrationReadinessCheck:
    m = config.migration
    assert m is not None
    stage = m.stages.runtime
    if stage.blockers:
        return MigrationReadinessCheck(
            "runtime",
            "BLOCKED",
            "missing deployed target receipt: " + ", ".join(stage.blockers),
        )
    check = _gated_stage(
        "runtime",
        True,
        stage.gate,
        ready_detail="deployed target and live verification approved",
        skipped_detail="",
    )
    if check.status != "READY":
        return check
    digest = migration_runtime_plan_document(config)["digest"]
    if digest not in stage.gate.evidence:
        return MigrationReadinessCheck(
            "runtime",
            "BLOCKED",
            f"gate evidence must include this exact plan digest: {digest}",
        )
    return MigrationReadinessCheck(
        "runtime",
        "READY",
        f"deployed target plan {digest} approved",
    )


def _network_receipt_blockers(migration: MigrationConfig) -> list[str]:
    blockers = list(migration.network.receipt.blockers)
    if (
        migration.network.ca_bundle_secret_name
        and not migration.network.receipt.ca_bundle_version_id
    ):
        blockers.append("ca_bundle_version_id")
    return blockers


def _network_stage_check(config: PlatformConfig) -> MigrationReadinessCheck:
    m = config.migration
    assert m is not None
    if not m.network.private_dependencies:
        return MigrationReadinessCheck(
            "networking",
            "NOT REQUIRED",
            "no private dependencies declared",
        )
    if not m.stages.runtime.account_id:
        return MigrationReadinessCheck(
            "networking",
            "BLOCKED",
            "missing migration.stages.runtime.account_id for the target VPC",
        )
    receipt_blockers = _network_receipt_blockers(m)
    if receipt_blockers:
        return MigrationReadinessCheck(
            "networking",
            "BLOCKED",
            "missing deployed network/probe receipt: " + ", ".join(receipt_blockers),
        )
    check = _gated_stage(
        "networking",
        True,
        m.network.gate,
        ready_detail=(
            f"{m.network.connectivity} path approved for "
            f"{len(m.network.private_dependencies)} private dependency(s)"
        ),
        skipped_detail="",
    )
    if check.status != "READY":
        return check
    digest = migration_network_plan_document(config)["digest"]
    if digest not in m.network.gate.evidence:
        return MigrationReadinessCheck(
            "networking",
            "BLOCKED",
            f"gate evidence must include this exact plan digest: {digest}",
        )
    return MigrationReadinessCheck(
        "networking",
        "READY",
        f"{m.network.connectivity} plan {digest} approved",
    )


def _data_stage_check(config: PlatformConfig) -> MigrationReadinessCheck:
    m = config.migration
    assert m is not None
    stage = m.stages.data
    check = _gated_stage(
        "data",
        stage.strategy != "none",
        stage.gate,
        ready_detail=f"{stage.strategy} evidence approved",
        skipped_detail="strategy is none",
    )
    if check.status != "READY":
        return check
    digest = migration_data_plan_document(config)["digest"]
    if digest not in stage.gate.evidence:
        return MigrationReadinessCheck(
            "data",
            "BLOCKED",
            f"gate evidence must include this exact plan digest: {digest}",
        )
    return MigrationReadinessCheck(
        "data",
        "READY",
        f"{stage.strategy} plan {digest} approved",
    )


def _trigger_stage_check(config: PlatformConfig) -> MigrationReadinessCheck:
    m = config.migration
    assert m is not None
    stage = m.stages.triggers
    check = _gated_stage(
        "triggers",
        stage.strategy != "none",
        stage.gate,
        ready_detail=f"{stage.strategy} evidence approved",
        skipped_detail="strategy is none",
    )
    if check.status != "READY":
        return check
    digest = migration_trigger_plan_document(config)["digest"]
    if digest not in stage.gate.evidence:
        return MigrationReadinessCheck(
            "triggers",
            "BLOCKED",
            f"gate evidence must include this exact plan digest: {digest}",
        )
    return MigrationReadinessCheck(
        "triggers",
        "READY",
        f"{stage.strategy} plan {digest} approved",
    )


def _traffic_stage_check(config: PlatformConfig) -> MigrationReadinessCheck:
    m = config.migration
    assert m is not None
    stage = m.stages.traffic
    check = _gated_stage(
        "traffic",
        stage.strategy != "none",
        stage.gate,
        ready_detail=f"{stage.strategy} evidence approved",
        skipped_detail="strategy is none; no cutover requested",
    )
    if check.status != "READY":
        return check
    digest = migration_traffic_plan_document(config)["digest"]
    if digest not in stage.gate.evidence:
        return MigrationReadinessCheck(
            "traffic",
            "BLOCKED",
            f"gate evidence must include this exact plan digest: {digest}",
        )
    traffic_approval = stage.gate.approved_at
    assert traffic_approval is not None
    prerequisite_approvals = [
        m.stages.runtime.gate.approved_at,
        (m.network.gate.approved_at if m.network.private_dependencies else None),
        (m.stages.data.gate.approved_at if m.stages.data.strategy != "none" else None),
        (
            m.stages.triggers.gate.approved_at
            if m.stages.triggers.strategy != "none"
            else None
        ),
    ]
    newer = [
        approved_at
        for approved_at in prerequisite_approvals
        if approved_at is not None and approved_at > traffic_approval
    ]
    if newer:
        return MigrationReadinessCheck(
            "traffic",
            "BLOCKED",
            "traffic approval predates a required runtime, network, data, or "
            "trigger approval",
        )
    return MigrationReadinessCheck(
        "traffic",
        "READY",
        f"{stage.strategy} plan {digest} approved",
    )


def _gated_stage(
    name: str,
    required: bool,
    gate: MigrationGate,
    *,
    ready_detail: str,
    skipped_detail: str,
) -> MigrationReadinessCheck:
    if not required:
        return MigrationReadinessCheck(name, "NOT REQUIRED", skipped_detail)
    if gate.ready:
        return MigrationReadinessCheck(name, "READY", ready_detail)
    return MigrationReadinessCheck(
        name,
        "BLOCKED",
        "missing " + ", ".join(gate.blockers),
    )


def migration_readiness(config: PlatformConfig) -> list[MigrationReadinessCheck]:
    """Return the fail-closed gates for shifting customer traffic.

    Runtime deployment is always in scope. Data, trigger, and traffic work is
    opt-in, while private connectivity becomes mandatory when dependencies
    are declared. This is pure configuration evaluation: external evidence is
    referenced, not fetched or trusted implicitly.
    """
    m = config.migration
    if m is None:
        return []

    checks = [
        _runtime_stage_check(config),
        _network_stage_check(config),
        _data_stage_check(config),
        _trigger_stage_check(config),
        _traffic_stage_check(config),
    ]
    return checks


def migration_data_ready(config: PlatformConfig) -> bool:
    """Whether the declared data stage is not required or fully approved."""
    m = config.migration
    if m is None:
        return False
    checks = {check.name: check for check in migration_readiness(config)}
    required = ["data"]
    if m.network.private_dependencies:
        required.append("networking")
    return all(checks[name].status in {"READY", "NOT REQUIRED"} for name in required)


def migration_data_plan_lines(config: PlatformConfig) -> list[str]:
    """Human-readable data plan with the approval-bound digest."""
    m = config.migration
    if m is None:
        return ["Migration data plan: no migration block."]
    stage = m.stages.data
    if stage.strategy == "none":
        return [
            "Migration data plan:",
            "  NOT REQUIRED: strategy is none; no customer data will be moved",
        ]

    document = migration_data_plan_document(config)
    lines = [
        "Migration data plan:",
        f"  Strategy: {stage.strategy}",
        f"  Digest: {document['digest']}",
    ]
    if stage.strategy == "retain-source":
        lines += [
            (
                "  Execution: no copy; the migrated runtime keeps the existing "
                "private datastore"
            ),
            "  Datasets:",
        ]
        lines += [
            (
                f"    {dataset.name}: {dataset.adapter} via {dataset.dependency}; "
                f"classification={dataset.classification_reference}; "
                f"retention={dataset.retention_reference}; "
                f"identity={dataset.identity_mapping_reference}; "
                f"validation={dataset.validation_reference}"
            )
            for dataset in sorted(stage.datasets, key=lambda item: item.name)
        ]
    else:
        lines.append(
            "  Execution: external only; this accelerator has no generic copy adapter"
        )

    data_check = {check.name: check for check in migration_readiness(config)}["data"]
    lines.append(f"  Gate: {data_check.status} — {data_check.detail}")
    if data_check.status != "READY":
        lines.append(
            "  Approval: add the exact digest above to gate.evidence with the "
            "customer evidence reference"
        )
    if m.network.private_dependencies:
        network_document = migration_network_plan_document(config)
        receipt_blockers = _network_receipt_blockers(m)
        network_check = {check.name: check for check in migration_readiness(config)}[
            "networking"
        ]
        lines += [
            "  Private-network prerequisite:",
            (
                f"    Digest: {network_document['digest']}"
                if not receipt_blockers
                else "    Digest: PENDING — populate the deployed network/probe "
                "receipt before approval"
            ),
            f"    Gate: {network_check.status} — {network_check.detail}",
        ]
    return lines


def migration_cutover_plan_lines(config: PlatformConfig) -> list[str]:
    """Human-readable runtime, network, trigger, and traffic approval plans."""
    m = config.migration
    if m is None:
        return ["Migration cutover plan: no migration block."]
    trigger = m.stages.triggers
    traffic = m.stages.traffic
    if trigger.strategy == "none" and traffic.strategy == "none":
        return [
            "Migration cutover plan:",
            "  NOT ENABLED: source trigger and traffic router remain unchanged",
        ]

    checks = {check.name: check for check in migration_readiness(config)}
    runtime = m.stages.runtime
    runtime_document = migration_runtime_plan_document(config)
    runtime_digest = (
        runtime_document["digest"]
        if not runtime.blockers
        else "PENDING — populate the deployed target receipt before approval"
    )
    lines = [
        "Migration cutover plan:",
        (
            f"  Runtime: account={runtime.account_id or '(missing)'}, "
            f"arn={runtime.runtime_arn or '(missing)'}"
        ),
        (
            f"    Artifact: source-hash={runtime.source_hash or '(missing)'}, "
            f"image={runtime.image_digest or '(missing)'}"
        ),
        f"    Verification: {runtime.verification_reference or '(missing)'}",
        f"    Digest: {runtime_digest}",
        f"    Gate: {checks['runtime'].status} — {checks['runtime'].detail}",
    ]
    if m.network.private_dependencies:
        network_document = migration_network_plan_document(config)
        network_receipt_blockers = _network_receipt_blockers(m)
        lines += [
            (
                f"  Network: {m.network.connectivity}, "
                f"{len(m.network.private_dependencies)} private dependency(s)"
            ),
            (
                f"    Digest: {network_document['digest']}"
                if not network_receipt_blockers
                else "    Digest: PENDING — populate the deployed network/probe "
                "receipt before approval"
            ),
            (
                f"    Gate: {checks['networking'].status} — "
                f"{checks['networking'].detail}"
            ),
        ]
    if trigger.strategy != "none":
        trigger_document = migration_trigger_plan_document(config)
        lines += [
            (
                f"  Trigger: {m.source.trigger}, {trigger.strategy}, "
                f"safety={trigger.safety_mode}"
            ),
            f"    Source: {trigger.source_reference}",
            f"    Shadow: {trigger.shadow_reference}",
            f"    Validation: {trigger.validation_reference}",
            f"    Digest: {trigger_document['digest']}",
            f"    Gate: {checks['triggers'].status} — {checks['triggers'].detail}",
        ]
    if traffic.strategy != "none":
        traffic_document = migration_traffic_plan_document(config)
        abort = traffic.abort
        assert abort is not None
        traffic_digest = (
            traffic_document["digest"]
            if not runtime.blockers
            and (not m.network.private_dependencies or not _network_receipt_blockers(m))
            else "PENDING — a required runtime or network receipt is incomplete"
        )
        lines += [
            f"  Traffic: {traffic.strategy} via {traffic.router_reference}",
            f"    Source: {traffic.source_reference}",
            f"    Target: {traffic.target_reference}",
            (
                "    Steps: "
                + (
                    ", ".join(f"{step}%" for step in traffic.canary_steps_percent)
                    if traffic.canary_steps_percent
                    else "atomic switch"
                )
            ),
            (
                f"    Abort: error>{abort.max_error_rate_percent}%, "
                f"p95>{abort.max_p95_latency_ms}ms, "
                f"failed-events>{abort.max_failed_events}; "
                f"observe {abort.observation_minutes}m"
            ),
            f"    Metrics: {traffic.metrics_reference}",
            f"    Digest: {traffic_digest}",
            f"    Gate: {checks['traffic'].status} — {checks['traffic'].detail}",
        ]
    lines.append(
        "  Execution: customer-operated; this command does not change an event "
        "source or router"
    )
    return lines


def migration_cutover_ready(config: PlatformConfig) -> bool:
    """True only when traffic cutover is explicitly requested and unblocked."""
    m = config.migration
    if m is None or m.stages.traffic.strategy == "none":
        return False
    return all(check.status != "BLOCKED" for check in migration_readiness(config))


def migration_readiness_lines(config: PlatformConfig) -> list[str]:
    """Human-readable cutover gate used by Design and deploy.sh."""
    checks = migration_readiness(config)
    if not checks:
        return ["Migration readiness: no migration block."]
    width = max(len(check.status) for check in checks)
    lines = ["Migration cutover readiness:"]
    lines += [
        f"  {check.status:<{width}}  {check.name}: {check.detail}" for check in checks
    ]
    if migration_cutover_ready(config):
        lines += ["  Overall: READY — approved for the declared traffic cutover"]
    elif config.migration.stages.traffic.strategy == "none":
        lines += [
            (
                "  Overall: NOT READY — traffic cutover is not enabled "
                "(safe target-only deployment)"
            )
        ]
    else:
        lines += ["  Overall: BLOCKED — do not shift customer traffic"]
    return lines


def migration_plan(config: PlatformConfig, account: str = "") -> list[str]:
    """Render the migration as plain text: what moves where, what the operator
    must create first, and how to check each private dependency. Pure — no
    I/O, no AWS — so deploy.sh and tests print the same thing."""
    m = config.migration
    if m is None:
        return ["No migration: block in this manifest — nothing to plan."]
    src, tgt, net = m.source, m.target, m.network
    origin = (
        f"image {src.image}"
        if src.image
        else f"build {src.build.context} ({src.build.dockerfile})"
    )
    lines = [
        f"Migration plan: {config.project}-{config.environment} ({config.region})",
        f"  Source: {src.platform}, {origin}, trigger {src.trigger}",
        (
            f"  Cutover: source trigger {src.trigger!r} is discovery metadata only; "
            "trigger/cutover infrastructure is external and is not deployed"
        ),
        (
            f"  Target: {tgt.runtime} runtime, {tgt.mode} mode "
            f"(deploys as {config.project}-{config.environment}-runtime-orchestrator)"
        ),
        (
            f"  agents.pattern {config.agents.pattern!r} is ignored: the runtime "
            "image comes from migration.source"
        ),
        "",
        "Stacks:",
    ]
    lines += [f"  {s}" for s in config.expected_stacks(account)]
    if tgt.mode == "adapter":
        base = f"http://127.0.0.1:{src.port}"
        lines += [
            "",
            "Adapter mapping (AgentCore contract -> your container):",
            f"  POST /invocations -> {base}{src.invoke_path}",
            f"  GET  /ping        -> {base}{src.health_path}",
        ]
    if src.env:
        lines += ["", "Environment (in clear):"]
        lines += [f"  {k}={v}" for k, v in src.env.items()]
    if src.secrets or src.registry_secret_name:
        lines += ["", "Secrets Manager (create these BEFORE deploying):"]
        lines += [
            f"  {name} <- {migration_secret_name(config, name)}" for name in src.secrets
        ]
        if src.registry_secret_name:
            lines.append(f"  registry login <- {src.registry_secret_name}")
    if net.private_dependencies:
        lines += [
            "",
            (
                f"Private dependencies (connectivity: {net.connectivity}); "
                "reachability check for each:"
            ),
            (
                "  External prerequisite: this plan records the VPN/Transit "
                "Gateway, DNS, and CA requirements; it does not provision or "
                "inject them."
            ),
        ]
        lines += [
            (
                f"  {host}: from a host in the VPC: "
                f"curl -sS -o /dev/null -w '%{{http_code}}' https://{host}/"
            )
            for host in net.private_dependencies
        ]
        lines.append(
            "  Automated check: deploy.sh verify invokes the fixed allow-list "
            "TLS probe from the runtime VPC"
        )
        if net.dns_forwarders:
            lines.append(f"  DNS forwarders: {', '.join(net.dns_forwarders)}")
        if net.ca_bundle_secret_name:
            lines.append(f"  CA bundle secret: {net.ca_bundle_secret_name}")
    if config.security.networking:
        lines += [
            "",
            (
                "Networking: VPC mode is on. Outbound traffic leaves through the "
                "NAT gateway EIP (networking stack output) — allow-list it on "
                "the customer side."
            ),
        ]
    if m.stages.data.strategy != "none":
        lines += ["", *migration_data_plan_lines(config)]
    if m.stages.triggers.strategy != "none" or m.stages.traffic.strategy != "none":
        lines += ["", *migration_cutover_plan_lines(config)]
    lines += ["", *migration_readiness_lines(config)]
    if m.warnings:
        lines += ["", "Warnings:"]
        lines += [f"  - {w}" for w in m.warnings]
    return lines


def sign_in_lines(config: PlatformConfig, account: str = "") -> list[str]:
    """The Design read-back for identity: who issues tokens, and what the
    customer must register on the IdP side BEFORE build — the redirect URI in
    brokered mode was derived by hand (and once for the wrong account) until
    this printed it."""
    ident = config.identity
    if ident.idp == "cognito":
        return ["  Sign-in: cognito"]
    if ident.mode == "direct":
        return [
            f"  Sign-in: {ident.idp} (direct — the IdP issues tokens; no Cognito)",
            f"    issuer: {ident.direct_issuer_url}",
            f"    audience: {ident.client_id}",
            (
                "    IdP app must: have a service principal in the tenant and "
                "request v2 access tokens (deploy.sh verify checks both)"
            ),
        ]
    acct = account or "<account-id>"
    redirect = (
        f"https://{config.project}-{config.environment}-{acct}.auth."
        f"{config.region}.amazoncognito.com/oauth2/idpresponse"
    )
    return [
        f"  Sign-in: {ident.idp} via Cognito (brokered)",
        f"    register this redirect URI on the IdP app: {redirect}",
    ]


def design_plan(config: PlatformConfig, account: str = "") -> list[str]:
    """The Design-phase read-back: everything this manifest will make the
    platform do, as plain text, before anything exists. Pure — no I/O, no AWS
    — so `deploy.sh design`, tests and docs print the same thing.

    Composes the pieces that already know their part (expected_stacks, the
    migration plan, warnings) rather than growing a second opinion of any.
    """
    dep = config.deployment
    try:
        role = config.federated_role(account)
    except Exception:  # noqa: BLE001 — an unlisted account is reported in the plan, not raised
        role = None
    stacks = (
        config.expected_stacks(account) if (dep.strategy != "federated" or role) else []
    )
    lines = [
        f"Design: {config.project}-{config.environment} in {config.region}",
        f"  Mode: {dep.mode}",
        f"  Topology: {dep.strategy}"
        + (f", this account is the {role} side" if role else "")
        + (f" (account {account})" if account else ""),
        *sign_in_lines(config, account),
        f"  Agent pattern: {config.agents.pattern}"
        + (f", model {config.agents.model_id}" if config.agents.model_id else "")
        + (" (allow-listed)" if config.agents.allowed_models else "")
        + (
            f", orchestrator runtime generation "
            f"{config.agents.orchestrator_runtime_generation} "
            "(controlled replacement)"
            if config.agents.orchestrator_runtime_generation > 1
            else ""
        ),
        "",
        f"Stacks ({len(stacks)}):"
        if stacks
        else "Stacks: (federated — deploy from a listed account to see a side)",
    ]
    lines += [f"  {s}" for s in stacks]

    on = [
        name
        for name, flag in (
            ("private networking (VPC mode)", config.security.networking),
            ("CloudTrail + alerting", config.security.cloudtrail_alerting),
            ("resource policies", config.security.resource_policies),
            ("egress filter", config.security.egress_filter),
            ("guardrail-enforced inference", config.security.require_guardrails),
            ("Cedar authorization", config.security.cedar.enabled),
            ("traceability", config.security.traceability),
            ("alarms + dashboard", config.observability.alarms),
        )
        if flag
    ]
    lines += [
        "",
        "Controls on: " + (", ".join(on) if on else "none (every control is opt-in)"),
        (
            "Lifecycle: retained stateful resources; "
            f"{config.observability.log_retention_days}-day logs; "
            f"{config.agents.memory.event_expiry_days}-day memory events"
            if dep.mode == "production"
            else "Lifecycle: disposable workshop resources"
        ),
    ]

    if config.use_cases:
        lines += ["", "Use cases:"]
        lines += [f"  {name}  (uc-{name})" for name in sorted(config.use_cases)]
    else:
        lines += ["", "Use cases: none yet (deploy.sh usecase new <name>)"]

    if config.migration:
        # migration_plan() prints the migration block's own warnings; the
        # identity/deployment ones are added below so none is lost.
        mig = migration_plan(config, account)
        if "Warnings:" in mig:
            migration_lines = mig[: mig.index("Warnings:")]
            while migration_lines and not migration_lines[-1]:
                migration_lines.pop()
        else:
            migration_lines = mig
        lines += [""] + migration_lines
    if config.warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in config.warnings]
    lines += [
        "",
        "Nothing has been deployed. Next: deploy.sh build, then deploy.sh verify.",
    ]
    return lines


def resolve_region(root: Path | None = None) -> str:
    """Resolve the deployment region the way deploy.sh does, so every tool
    (verify, invoke, monitor, check_*) reports on the region the deploy
    actually used instead of silently defaulting to us-east-1:

        env AWS_REGION/AWS_DEFAULT_REGION > platform.yaml > workshop.env
        > AWS profile > us-east-1

    `root` overrides the repository root (tests); PLATFORM_CONFIG overrides
    the manifest path, mirroring deploy.sh.
    """
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region
    root = root or Path(__file__).resolve().parents[1]
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or root / "platform.yaml")
    if manifest.exists():
        try:
            return load_platform_config(manifest).region
        except Exception:  # noqa: BLE001, S110 — deploy fails loudly on a bad
            pass  # manifest; a read-only tool just falls through the chain.
    saved = root / "workshop.env"
    if saved.exists():
        match = re.search(r"^AWS_REGION=([a-z0-9-]+)$", saved.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    import boto3  # lazy: only the fallback needs it

    return boto3.Session().region_name or "us-east-1"


def _main() -> int:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if (
        flags
        & {
            "--plan",
            "--design",
            "--readiness",
            "--data-plan",
            "--data-readiness",
            "--cutover-plan",
            "--cutover-readiness",
        }
        and not args
    ):
        # --plan alone reads the deployment manifest, like deploy.sh does.
        root = Path(__file__).resolve().parents[1]
        args = [os.environ.get("PLATFORM_CONFIG") or str(root / "platform.yaml")]
    action_flags = flags & {
        "--export",
        "--stacks",
        "--plan",
        "--design",
        "--readiness",
        "--data-plan",
        "--data-readiness",
        "--cutover-plan",
        "--cutover-readiness",
    }
    if (
        len(args) != 1
        or len(action_flags) > 1
        or not flags
        <= {
            "--export",
            "--stacks",
            "--plan",
            "--design",
            "--readiness",
            "--data-plan",
            "--data-readiness",
            "--cutover-plan",
            "--cutover-readiness",
            "--effective-env",
        }
        or ("--effective-env" in flags and not action_flags)
    ):
        print(
            "usage: python -m infra_utils.platform_config "
            "[--export | --stacks | --plan | --design | --readiness | "
            "--data-plan | --data-readiness | --cutover-plan | "
            "--cutover-readiness] "
            "[--effective-env] <platform.yaml>",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_platform_config(args[0])
    except ValidationError as exc:
        # Messages only — pydantic's str() echoes the offending input, and for
        # identity.client_secret_name the input may BE the secret.
        print(f"INVALID: {args[0]}", file=sys.stderr)
        for err in exc.errors():
            where = ".".join(str(x) for x in err["loc"]) or "platform.yaml"
            print(
                f"  {where}: {err['msg'].removeprefix('Value error, ')}",
                file=sys.stderr,
            )
        return 1
    except Exception as exc:  # noqa: BLE001 — YAML syntax, missing file: print it all
        print(f"INVALID: {args[0]}\n{exc}", file=sys.stderr)
        return 1
    if "--effective-env" in flags:
        try:
            config = apply_environment_overrides(config)
        except (ValidationError, ValueError) as exc:
            print(f"INVALID effective environment\n{exc}", file=sys.stderr)
            return 1
    if "--export" not in flags:
        # --export is machine-read by deploy.sh with 2>&1; keep it key=value only.
        for warning in config.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    if "--export" in flags:
        for key, value in to_env(config).items():
            print(f"{key}={value}")
        return 0
    if "--stacks" in flags:
        # The contract's view of the footprint; CDK_DEFAULT_ACCOUNT selects
        # the side of a federated deployment, same as app.py.
        for name in config.expected_stacks(os.environ.get("CDK_DEFAULT_ACCOUNT", "")):
            print(name)
        return 0
    if "--design" in flags:
        print("\n".join(design_plan(config, os.environ.get("CDK_DEFAULT_ACCOUNT", ""))))
        findings = region_findings(config)
        if findings:
            print("\nRegion checks (static tables, no AWS calls):")
            for finding in findings:
                print(f"  {finding.render()}")
        return 1 if any(f.level == "error" for f in findings) else 0
    if "--plan" in flags:
        print(
            "\n".join(migration_plan(config, os.environ.get("CDK_DEFAULT_ACCOUNT", "")))
        )
        return 0
    if "--readiness" in flags:
        print("\n".join(migration_readiness_lines(config)))
        return 0 if migration_cutover_ready(config) else 1
    if "--data-plan" in flags:
        print("\n".join(migration_data_plan_lines(config)))
        return 0
    if "--data-readiness" in flags:
        print("\n".join(migration_data_plan_lines(config)))
        return 0 if migration_data_ready(config) else 1
    if "--cutover-plan" in flags:
        print("\n".join(migration_cutover_plan_lines(config)))
        return 0
    if "--cutover-readiness" in flags:
        print("\n".join(migration_cutover_plan_lines(config)))
        print()
        print("\n".join(migration_readiness_lines(config)))
        return 0 if migration_cutover_ready(config) else 1
    print(f"OK: {args[0]}")
    print(config.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
