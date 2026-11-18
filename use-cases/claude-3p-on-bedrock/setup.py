#!/usr/bin/env python3
"""Setup wizard for claude-3p-on-bedrock: every step before `deploy.sh deploy`.

    python use-cases/claude-3p-on-bedrock/setup.py            # interactive wizard
    python use-cases/claude-3p-on-bedrock/setup.py check      # read-only re-check
    python use-cases/claude-3p-on-bedrock/setup.py --answers ~/.claude-3p-on-bedrock/<p>-<e>.answers.yaml

It covers the AWS-side tasks of docs/walkthrough.md and the platform settings the use
case depends on:

  0. platform    Region, account pin, security.networking, a platform model in the Region
  1. hostname    hostname, DNS mode (customer CNAME or Route 53 private zone), client CIDRs
  2. certificate existing ACM cert | NEW ACM public cert (DNS validation) | import | test-only CA
  3. Entra ID    tenant, app, email domains; prints the portal checklist; optional read-only
                 check of the app registration with the Azure CLI (az)
  4. secret      the Entra client secret into Secrets Manager (hidden input, never printed)
  5. models      pick from the Anthropic inference profiles ACTIVE in the Region
  6. teams       Entra App Role -> models, plus the default policy
  7. write       shows the use-case block, backs up platform.yaml, writes on confirmation
  8. check       same as `setup.py check`: PASS/WARN/FAIL for everything the deploy needs

Rules: nothing in AWS is created or changed without an explicit "y"; every step
can be re-run; no secret value is printed or written to disk outside the
private temp handling of the test-only CA. Answers (no secrets) are saved for a
re-run or a second environment.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv lists only (openssl, az)
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
USE_CASE = "claude-3p-on-bedrock"
PLATFORM_VPC = ipaddress.ip_network(
    "10.0.0.0/16"
)  # fixed by the networking stack today
DEFAULT_TRUSTED_PROXIES = ["10.0.2.0/24", "10.0.3.0/24"]
STABLE_URL = "https://downloads.claude.ai/claude-code-releases/stable"
ANSWERS_DIR = Path.home() / ".claude-3p-on-bedrock"
# Backups of platform.yaml live here, not next to it: a .bak in the repository
# is not gitignored and would carry the customer's values into a commit.
BACKUP_DIR = ANSWERS_DIR / "backups"
# What the wizard asks for in a team or the default policy. Every other key of
# an existing block (settings, desktop_settings, start_model, ...) is kept.
_WIZARD_POLICY_KEYS = ("models", "deny_tools")
_WIZARD_TEAM_KEYS = ("name", "entra_roles", *_WIZARD_POLICY_KEYS)

# Keys of the use-case block the wizard sets (or deliberately removes) itself.
_WIZARD_OWNED_KEYS = (
    "claude_version",
    "hostname",
    "dns",
    "certificate_arn",
    "certificate_private_ca",
    "client_cidrs",
    "trusted_proxies",
    "oidc",
    "models",
    "teams",
    "default",
    "agentcore_tools",
)

GENERIC_ANSWER_KEYS = (
    "region",
    "platform_model_id",
    "models",
    "teams",
    "default",
    "secret_name",
)


def backup_path(manifest: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ANSWERS_DIR.chmod(0o700)
    BACKUP_DIR.chmod(0o700)
    return BACKUP_DIR / f"{manifest.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"


def _load_gc():
    name = "use_case_claude_3p_on_bedrock_config"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, HERE / "gateway" / "config.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


gc = _load_gc()


# ── pure helpers (unit tested) ───────────────────────────────────────────────


def model_id_from_profile(profile_id: str) -> str:
    """eu.anthropic.claude-haiku-4-5-20251001-v1:0 -> claude-haiku-4-5."""
    fm = gc.foundation_model_id(profile_id) or profile_id
    name = fm.split(".", 1)[1] if fm.startswith("anthropic.") else fm
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    return re.sub(r"-\d{8}$", "", name)


def model_label(model_id: str) -> str:
    """claude-haiku-4-5 -> Claude Haiku 4.5; claude-opus-5 -> Claude Opus 5."""
    parts = model_id.removeprefix("claude-").split("-")
    words = [p.capitalize() for p in parts if not p.isdigit()]
    nums = [p for p in parts if p.isdigit()]
    return " ".join(["Claude", *words] + ([".".join(nums)] if nums else []))


def cert_covers(hostname: str, names: list[str]) -> bool:
    """True when a certificate name (exact or single-label wildcard) covers hostname."""
    host = hostname.lower()
    for n in (x.lower() for x in names):
        if n == host:
            return True
        if (
            n.startswith("*.")
            and host.count(".") >= 2
            and host.split(".", 1)[1] == n[2:]
        ):
            return True
    return False


# Suffixes that never resolve in public DNS, so public ACM validation can't work.
_INTERNAL_SUFFIXES = (
    ".internal",
    ".local",
    ".localhost",
    ".lan",
    ".home.arpa",
    ".corp",
    ".intranet",
    ".private",
    ".test",
    ".example",
    ".invalid",
)


def valid_role(value: str) -> bool:
    """Entra App Role values are non-empty and contain no whitespace."""
    return bool(value.strip()) and not re.search(r"\s", value)


def roles_ok(values: list[str]) -> str | None:
    if not values:
        return "at least one role"
    bad = [v for v in values if not valid_role(v)]
    return f"not an Entra App Role value (no spaces allowed): {bad}" if bad else None


def readable_errors(err: Exception) -> list[str]:
    """pydantic ValidationError -> 'field.path: message' lines, no traceback."""
    errors = getattr(err, "errors", None)
    if not callable(errors):
        return [str(err)]
    out = []
    for e in errors():
        loc = ".".join(str(x) for x in e.get("loc", ()))
        msg = str(e.get("msg", "")).removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def region_geo_prefix(region: str) -> str:
    """eu-west-1 -> eu; us-east-1 -> us; ap-southeast-2 -> apac (inference-profile geos)."""
    head = region.split("-", 1)[0]
    return {"ap": "apac"}.get(head, head)


def is_public_name(hostname: str) -> bool:
    return not hostname.lower().rstrip(".").endswith(_INTERNAL_SUFFIXES)


def overlaps_platform_vpc(cidrs: list[str]) -> list[str]:
    return [
        c for c in cidrs if ipaddress.ip_network(c, strict=False).overlaps(PLATFORM_VPC)
    ]


def best_public_zone(hostname: str, zones: list[dict]) -> dict | None:
    """Longest public hosted zone whose name is a suffix of hostname."""
    host = hostname.rstrip(".").lower() + "."
    matches = [
        z
        for z in zones
        if not z.get("Config", {}).get("PrivateZone")
        and (host == z["Name"] or host.endswith("." + z["Name"]))
    ]
    return max(matches, key=lambda z: len(z["Name"]), default=None)


def render_block(cfg: dict) -> str:
    body = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False, width=100)
    indented = "".join(
        f"    {line}" if line.strip() else line
        for line in body.splitlines(keepends=True)
    )
    return f"  {USE_CASE}:\n" + indented


def _flow_use_cases_to_block(lines: list[str], top: int, text: str) -> None:
    """`use_cases: {a: {}, b: {x: 1}}` (one or several lines) -> block style, in
    place, keeping every other use case. Comments inside the flow mapping are not
    kept. Anything that isn't a mapping is refused, never silently rewritten."""
    root = yaml.compose(text)
    value = next(
        (
            v
            for k, v in (root.value if isinstance(root, yaml.MappingNode) else [])
            if k.value == "use_cases"
        ),
        None,
    )
    existing = (yaml.safe_load(text) or {}).get("use_cases")
    if not isinstance(existing, dict) or value is None:
        raise SystemExit(
            "stopped: platform.yaml unchanged. use_cases is written in a form the "
            "wizard can't edit; make it a block mapping (use_cases:, then names "
            "indented below it) and run again."
        )
    others = {k: v for k, v in existing.items() if k != USE_CASE}
    section = ["use_cases:\n"]
    if others:
        body = yaml.safe_dump(
            others, sort_keys=False, default_flow_style=False, width=100
        )
        section += [
            f"  {line}" if line.strip() else line
            for line in body.splitlines(keepends=True)
        ]
    # One element per line: the caller walks the section line by line.
    lines[top : value.end_mark.line + 1] = section


def upsert_use_case_block(text: str, cfg: dict) -> str:
    """Replace (or add) `use_cases: claude-3p-on-bedrock:` textually, keeping
    every other line and comment of platform.yaml exactly as it was."""
    block = render_block(cfg)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    top = next((i for i, l in enumerate(lines) if re.match(r"^use_cases:", l)), None)
    if top is None:
        return (
            "".join(lines)
            + ("\n" if lines and lines[-1].strip() else "")
            + "use_cases:\n"
            + block
        )
    after = re.sub(r"\s+#.*$", "", lines[top].split(":", 1)[1]).strip()
    if after in ("{}", "null", "~"):
        lines[top] = "use_cases:\n"
    elif after:
        # Flow style with content: convert, or the section below would be skipped
        # and a second `use_cases:` key would silently replace the first.
        _flow_use_cases_to_block(lines, top, text)
    end = top + 1  # end of the use_cases section: next column-0 key
    while end < len(lines) and (
        not lines[end].strip() or lines[end].startswith((" ", "#"))
    ):
        end += 1
    start = next(
        (
            i
            for i in range(top + 1, end)
            if re.match(rf"^  {re.escape(USE_CASE)}:", lines[i])
        ),
        None,
    )
    if start is None:
        insert_at = end
        while insert_at > top + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        return "".join(lines[:insert_at] + [block] + lines[insert_at:])
    stop = start + 1
    while stop < end and (not lines[stop].strip() or lines[stop].startswith("    ")):
        stop += 1
    while stop > start + 1 and not lines[stop - 1].strip():
        stop -= 1
    return "".join(lines[:start] + [block] + lines[stop:])


def replace_in_section(text: str, section: str, old: str, new: str) -> str:
    """Replace the whole-token `old` by `new` inside the top-level `section:` only."""
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, l in enumerate(lines) if re.match(rf"^{re.escape(section)}:", l)),
        None,
    )
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and (
        not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))
    ):
        end += 1
    token = re.compile(rf"(?<![\w.:-]){re.escape(old)}(?![\w.:-])")
    lines[start:end] = [token.sub(new, l) for l in lines[start:end]]
    return "".join(lines)


def set_top_scalar(text: str, key: str, value: str) -> str | None:
    pat = re.compile(rf"^{re.escape(key)}:[^\n]*$", re.MULTILINE)
    return pat.sub(f"{key}: {value}", text, count=1) if pat.search(text) else None


def set_nested_scalar(
    text: str, parent: str, key: str, value: str, comment: str = ""
) -> str | None:
    """Set `parent: key: value` (2-space block style, or an inline `parent: {}`)."""
    tail = f"   # {comment}" if comment else ""
    inline = re.compile(rf"^{re.escape(parent)}:\s*\{{\s*\}}[^\n]*$", re.MULTILINE)
    if inline.search(text):
        return inline.sub(f"{parent}:\n  {key}: {value}{tail}", text, count=1)
    block = re.compile(
        rf"^{re.escape(parent)}:[ \t]*(#[^\n]*)?\n((?:[ \t]+[^\n]*\n|\n)*)",
        re.MULTILINE,
    )
    m = block.search(text)
    if not m:
        return None
    body = m.group(2)
    child = re.compile(rf"^  {re.escape(key)}:[^\n]*$", re.MULTILINE)
    new_body = (
        child.sub(f"  {key}: {value}{tail}", body, count=1)
        if child.search(body)
        else f"  {key}: {value}{tail}\n" + body
    )
    return text[: m.start(2)] + new_body + text[m.end(2) :]


# ── terminal interaction ─────────────────────────────────────────────────────


class UI:
    def __init__(self, interactive: bool, assume_yes: bool):
        self.interactive = interactive
        self.assume_yes = assume_yes

    def ask(self, prompt: str, default: str | None = None, validate=None) -> str:
        while True:
            if not self.interactive:
                if default is None:
                    raise SystemExit(
                        f"FAIL: no answer for {prompt!r} (non-interactive; add it to --answers)"
                    )
                value = default
            else:
                shown = f" [{default}]" if default not in (None, "") else ""
                value = input(f"  {prompt}{shown}: ").strip() or (default or "")
            if validate is None:
                return value
            err = validate(value)
            if not err:
                return value
            if not self.interactive:
                raise SystemExit(f"FAIL: {prompt}: {err}")
            print(f"    ! {err}")

    def ask_list(
        self, prompt: str, default: list[str] | None = None, validate=None
    ) -> list[str]:
        # An explicit empty default ("" ) is a complete answer; only a missing
        # default (None) makes a non-interactive run stop.
        raw = self.ask(prompt, None if default is None else ", ".join(default), None)
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if validate:
            err = validate(items)
            if err:
                if not self.interactive:
                    raise SystemExit(f"FAIL: {prompt}: {err}")
                print(f"    ! {err}")
                return self.ask_list(prompt, default, validate)
        return items

    def confirm(self, prompt: str, default: bool = False) -> bool:
        if self.assume_yes:
            print(f"  {prompt} [yes: --yes]")
            return True
        if not self.interactive:
            print(f"  {prompt} [no: non-interactive without --yes]")
            return False
        ans = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not ans else ans in ("y", "yes")

    def choose(self, prompt: str, options: list[str], default: int = 1) -> int:
        for i, o in enumerate(options, 1):
            print(f"    {i}. {o}")
        v = self.ask(
            prompt,
            str(default),
            lambda x: (
                None
                if x.isdigit() and 1 <= int(x) <= len(options)
                else f"pick 1-{len(options)}"
            ),
        )
        return int(v)


def header(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))


# ── AWS context ──────────────────────────────────────────────────────────────


class Aws:
    def __init__(self, region: str):
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        self.region = region
        self.s = boto3.session.Session(region_name=region)
        try:
            ident = self.s.client("sts").get_caller_identity()
        except (BotoCoreError, ClientError) as e:
            profile = os.environ.get("AWS_PROFILE") or "<your-profile>"
            raise SystemExit(
                f"FAIL: no valid AWS credentials in this terminal ({e.__class__.__name__}).\n"
                f"  Sign in first, then run the wizard again:\n"
                f"    aws sso login --profile {profile}\n"
                f"    export AWS_PROFILE={profile}\n"
                f"  (or however your organization provides AWS credentials)"
            ) from None
        self.account = ident["Account"]
        self.identity = ident["Arn"]

    def client(self, name: str, region: str | None = None):
        return self.s.client(name, region_name=region or self.region)

    def anthropic_profiles(self) -> list[str]:
        out: list[str] = []
        for page in (
            self.client("bedrock").get_paginator("list_inference_profiles").paginate()
        ):
            for p in page["inferenceProfileSummaries"]:
                if (
                    "anthropic" in p["inferenceProfileId"]
                    and p.get("status") == "ACTIVE"
                ):
                    out.append(p["inferenceProfileId"])
        return sorted(out)

    def certificates(self) -> list[dict]:
        acm = self.client("acm")
        certs = []
        for page in acm.get_paginator("list_certificates").paginate(
            CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]
        ):
            for s in page["CertificateSummaryList"]:
                names = [s.get("DomainName", "")] + list(
                    s.get("SubjectAlternativeNameSummaries", [])
                )
                certs.append({**s, "names": sorted({n for n in names if n})})
        return certs


# ── wizard ───────────────────────────────────────────────────────────────────


class Wizard:
    def __init__(self, manifest: Path, ui: UI, answers: dict):
        self.manifest = manifest
        self.ui = ui
        self.answers = answers
        self.text = manifest.read_text()
        self.raw = yaml.safe_load(self.text) or {}
        self.existing = ((self.raw.get("use_cases") or {}).get(USE_CASE)) or {}
        self.aws: Aws | None = None
        self.cfg: dict[str, Any] = {}

    # defaults: answers file > existing block > built-in
    def default_for(self, key: str, fallback=None):
        if key in self.answers:
            return self.answers[key]
        return self.existing.get(key, fallback)

    # 0 ─────────────────────────────────────────────────────────────────────
    def step_platform(self) -> None:
        header("0. Platform settings this use case depends on")
        region = self.ui.ask(
            "AWS Region to deploy to",
            self.answers.get("region") or self.raw.get("region") or "eu-west-1",
            lambda v: (
                None
                if re.match(r"^[a-z]{2}(-gov)?-[a-z]+-\d$", v)
                else "not a Region name"
            ),
        )
        self.aws = Aws(region)
        print(f"  signed in to AWS account {self.aws.account}")
        print(f"  as {self.aws.identity}")
        if not self.ui.confirm(
            f"Is {self.aws.account} the account to deploy to? (If not, stop and sign in to the right one)",
            True,
        ):
            raise SystemExit(
                "stopped: sign in to the target account (aws sso login --profile ...) and run again"
            )
        new = self.text
        if self.raw.get("region") != region:
            new = set_top_scalar(new, "region", region) or new + f"\nregion: {region}\n"
        pinned = str((self.raw.get("deployment") or {}).get("platform_account", ""))
        if pinned and pinned != self.aws.account:
            raise SystemExit(
                f"FAIL: platform.yaml is pinned to account {pinned}, credentials are for {self.aws.account}"
            )
        if not pinned and self.ui.confirm(
            f"Pin platform.yaml to account {self.aws.account} (deploys refuse any other)?",
            True,
        ):
            new = (
                set_nested_scalar(
                    new, "deployment", "platform_account", f'"{self.aws.account}"'
                )
                or new
            )
        if not (self.raw.get("security") or {}).get("networking"):
            print(
                "  The gateway runs inside the platform's private network (VPC), so this use case\n"
                "  needs security.networking: true. With it, the deployment includes the EBA\n"
                "  networking stack (<project>-<env>-networking): a VPC 10.0.0.0/16 with private\n"
                "  subnets, a NAT gateway and VPC endpoints (a few USD per day). This only adds the\n"
                "  stack to platform.yaml; nothing is created until you deploy."
            )
            if self.ui.confirm(
                "Turn on security.networking (the networking stack will be deployed)?",
                True,
            ):
                new = (
                    set_nested_scalar(
                        new,
                        "security",
                        "networking",
                        "true",
                        "VPC the use case runs in",
                    )
                    or new
                )
        profiles = self.aws.anthropic_profiles()
        model = (self.raw.get("agents") or {}).get(
            "model_id"
        ) or "us.anthropic.claude-sonnet-4-6"
        if model not in profiles:
            print(
                f"  The platform's own sample agent (the EBA orchestrator runtime) uses\n"
                f"  {model!r} by default, which isn't offered in {region} (doctor would FAIL).\n"
                f"  Pick ONE model for it. This is not the Claude Code / Cowork catalog:\n"
                f"  you choose those models, several of them, in step 5."
            )
            geo = region_geo_prefix(region)
            sonnet = [p for p in profiles if "sonnet" in p]
            local = [p for p in sonnet if p.startswith(geo + ".")]
            pick = self.ui.ask(
                "Model for the platform's sample agent (one profile ID)",
                self.answers.get("platform_model_id")
                or (local or sonnet or profiles)[-1],
                lambda v: (
                    None
                    if v in profiles
                    else "enter exactly ONE ACTIVE profile ID, e.g. "
                    + ((local or sonnet or profiles)[-1])
                ),
            )
            new = (
                set_nested_scalar(
                    new, "agents", "model_id", pick, "available in this Region"
                )
                or new
            )
            allowed = (self.raw.get("agents") or {}).get("allowed_models") or []
            if model in allowed:
                # agents.model_id must be in the allow-list, and the allow-list is
                # a deliberate IAM boundary: swap only the entry that this Region
                # can't serve, and say so.
                print(
                    f"  agents.allowed_models pins {model!r}. The platform requires\n"
                    f"  agents.model_id to be in it, so {pick!r} must take its place."
                )
                if not self.ui.confirm(
                    f"Replace {model} with {pick} in agents.allowed_models?", True
                ):
                    raise SystemExit(
                        "stopped: platform.yaml unchanged. Set agents.model_id and "
                        "agents.allowed_models to the same available model, then run again."
                    )
                new = replace_in_section(new, "agents", model, pick)
            self.answers["platform_model_id"] = pick
        self.answers["region"] = region
        self._write_if_changed(new, "platform settings")

    # 1 ─────────────────────────────────────────────────────────────────────
    def step_hostname(self) -> None:
        header(
            "1. Hostname, DNS and corporate networks (docs/walkthrough.md: Choose the hostname)"
        )
        host_re = re.compile(gc._HOST)
        conf = self.cfg
        conf["hostname"] = self.ui.ask(
            "Gateway hostname developers use (e.g. claude.corp.example.com)",
            self.answers.get("hostname"),
            lambda v: None if host_re.match(v) else "not a valid DNS name",
        )
        dns = self.default_for("dns", {}) or {}
        mode = ["customer", "route53"][
            self.ui.choose(
                "DNS mode",
                [
                    "customer: a CNAME in the customer's corporate DNS (recommended)",
                    "route53: a Route 53 private hosted zone on the platform VPC",
                ],
                2 if dns.get("mode") == "route53" else 1,
            )
            - 1
        ]
        conf["dns"] = {"mode": mode}
        if mode == "route53":
            zone = (dns.get("private_zone") or {}).get("name") or conf[
                "hostname"
            ].split(".", 1)[1]
            conf["dns"]["private_zone"] = {
                "name": self.ui.ask("Private zone name", zone)
            }

        def cidrs_ok(items):
            try:
                bad = [
                    x
                    for x in items
                    if ipaddress.ip_network(x, strict=True).version != 4
                ]
            except ValueError as e:
                return str(e)
            if not items:
                return "at least one CIDR"
            if bad:
                return f"IPv4 only: {bad}"
            broad = [c for c in items if gc.too_broad(c)]
            if broad:
                return (
                    f"{broad} are too broad (wider than /{gc.MIN_CLIENT_PREFIX}): "
                    "list the networks developers connect from"
                )
            clash = overlaps_platform_vpc(items)
            return (
                f"{clash} overlap the platform VPC {PLATFORM_VPC} (see docs/walkthrough.md: Connect the corporate network)"
                if clash
                else None
            )

        conf["client_cidrs"] = self.ui.ask_list(
            "Networks developers connect from, comma-separated CIDRs (e.g. 172.16.0.0/16)",
            self.answers.get("client_cidrs"),
            cidrs_ok,
        )
        conf["trusted_proxies"] = self.default_for(
            "trusted_proxies", DEFAULT_TRUSTED_PROXIES
        )

    # 2 ─────────────────────────────────────────────────────────────────────
    def step_certificate(self) -> None:
        header("2. TLS certificate in ACM (docs/walkthrough.md: Get a certificate)")
        host = self.cfg["hostname"]
        certs = [x for x in self.aws.certificates() if cert_covers(host, x["names"])]
        opts, actions = [], []
        for x in certs:
            opts.append(
                f"use existing {x['Status']} {x.get('Type', '')} {', '.join(x['names'])}  ({x['CertificateArn'][-12:]})"
            )
            actions.append(("existing", x))
        public = is_public_name(host)
        if public:
            opts.append(
                "request a NEW ACM public certificate (DNS validation; free, auto-renews)"
            )
            actions.append(("request", None))
        else:
            print(
                f"  {host} is under an internal-only domain: a public ACM certificate can't be\n"
                "  validated for it, so that option is not offered. Use your PKI or the test CA."
            )
        opts += [
            "import a certificate from your PKI (PEM files)",
            "TEST ONLY: create a local test CA and import a certificate signed by it",
        ]
        actions += [("import", None), ("testca", None)]
        current = self.default_for("certificate_arn")
        # Default: the certificate already in platform.yaml, else any existing
        # match, else a new public certificate (public names) or import.
        default = next(
            (
                i
                for i, (k, x) in enumerate(actions, 1)
                if k == "existing" and x["CertificateArn"] == current
            ),
            1,
        )
        kind, cert = actions[self.ui.choose("Certificate", opts, default) - 1]
        self.cfg["certificate_private_ca"] = False
        if kind == "existing":
            self.cfg["certificate_arn"] = cert["CertificateArn"]
            self.cfg["certificate_private_ca"] = cert.get(
                "Type"
            ) == "IMPORTED" and bool(self.default_for("certificate_private_ca", False))
            if cert.get("Type") == "IMPORTED":
                self.cfg["certificate_private_ca"] = self.ui.confirm(
                    "Is this imported certificate signed by a private CA?",
                    bool(self.default_for("certificate_private_ca", False)),
                )
            if cert["Status"] == "PENDING_VALIDATION":
                self._acm_validation(cert["CertificateArn"])
        elif kind == "request":
            self._acm_request(host)
        elif kind == "import":
            self._acm_import()
        else:
            self._test_ca(host)

    def _acm_request(self, host: str) -> None:
        if not self.ui.confirm(
            f"Request a public ACM certificate for {host} in {self.aws.region}?"
        ):
            raise SystemExit("stopped: no certificate")
        arn = self.aws.client("acm").request_certificate(
            DomainName=host,
            ValidationMethod="DNS",
            Tags=[{"Key": "purpose", "Value": USE_CASE}],
        )["CertificateArn"]
        print(f"  requested {arn}")
        self.cfg["certificate_arn"] = arn
        self._acm_validation(arn)

    def _acm_validation(self, arn: str) -> None:
        acm = self.aws.client("acm")
        rec = None
        for _ in range(
            20
        ):  # the validation record appears a few seconds after the request
            opts = acm.describe_certificate(CertificateArn=arn)["Certificate"].get(
                "DomainValidationOptions", []
            )
            rec = opts[0].get("ResourceRecord") if opts else None
            if rec:
                break
            time.sleep(3)
        if not rec:
            print(
                "  ! ACM hasn't published the validation record yet; re-run the wizard in a minute."
            )
            return
        print(
            "  Add this record in the PUBLIC DNS zone of the domain (validation only; the ALB stays internal):"
        )
        print(f"    {rec['Name']}  {rec['Type']}  {rec['Value']}")
        r53 = self.aws.client("route53")
        zones = [
            z
            for p in r53.get_paginator("list_hosted_zones").paginate()
            for z in p["HostedZones"]
        ]
        zone = best_public_zone(
            rec["Name"].rstrip(".").split(".", 1)[1]
            if rec["Name"].startswith("_")
            else rec["Name"],
            zones,
        )
        if zone and self.ui.confirm(
            f"Public zone {zone['Name']} is in this account. Create the record there?",
            True,
        ):
            r53.change_resource_record_sets(
                HostedZoneId=zone["Id"],
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "UPSERT",
                            "ResourceRecordSet": {
                                "Name": rec["Name"],
                                "Type": rec["Type"],
                                "TTL": 300,
                                "ResourceRecords": [{"Value": rec["Value"]}],
                            },
                        }
                    ]
                },
            )
            print("  record created")
        elif not zone:
            print(
                "  No public hosted zone for this domain in the account: hand the record to the DNS team."
            )
        if self.ui.confirm(
            "Wait up to 10 minutes for ACM to issue the certificate?", True
        ):
            try:
                acm.get_waiter("certificate_validated").wait(
                    CertificateArn=arn, WaiterConfig={"Delay": 15, "MaxAttempts": 40}
                )
                print("  ISSUED")
            except Exception:  # noqa: BLE001 - timeout or failed validation: report, continue
                print(
                    "  ! not issued yet. `setup.py check` reports the status; deploy only once it is ISSUED."
                )

    def _acm_import(self) -> None:
        def readable(p):
            return None if Path(p).expanduser().is_file() else "file not found"

        cert = Path(self.ui.ask("Certificate PEM file", validate=readable)).expanduser()
        key = Path(
            self.ui.ask(
                "Private key PEM file (read locally, sent only to ACM)",
                validate=readable,
            )
        ).expanduser()
        chain = self.ui.ask("Chain PEM file (empty if none)", "")
        if not self.ui.confirm(f"Import {cert.name} into ACM in {self.aws.region}?"):
            raise SystemExit("stopped: no certificate")
        args = {
            "Certificate": cert.read_bytes(),
            "PrivateKey": key.read_bytes(),
            "Tags": [{"Key": "purpose", "Value": USE_CASE}],
        }
        if chain:
            args["CertificateChain"] = Path(chain).expanduser().read_bytes()
        self.cfg["certificate_arn"] = self.aws.client("acm").import_certificate(**args)[
            "CertificateArn"
        ]
        self.cfg["certificate_private_ca"] = self.ui.confirm(
            "Signed by a private CA (laptops must trust it)?", False
        )
        print(f"  imported {self.cfg['certificate_arn']}")
        print("  Imported certificates do NOT auto-renew: re-import before expiry.")

    def _test_ca(self, host: str) -> None:
        print(
            "  TEST ONLY. Laptops must trust this CA, and it never renews. Not for customers."
        )
        if not shutil.which("openssl"):
            raise SystemExit("FAIL: openssl not found")
        ca_dir = Path(
            self.ui.ask(
                "Directory for the test CA and keys",
                str(Path.home() / ".claude-3p-test-pki" / host),
            )
        ).expanduser()
        if not self.ui.confirm(
            f"Create a test CA in {ca_dir} and import a certificate for {host} into ACM?"
        ):
            raise SystemExit("stopped: no certificate")
        ca_dir.mkdir(parents=True, exist_ok=True)
        ca_dir.chmod(0o700)
        old = os.umask(0o077)
        try:
            run = lambda *a: subprocess.run(
                list(a), cwd=ca_dir, check=True, capture_output=True
            )  # nosec B603 - fixed openssl argv, no shell, no user-controlled command
            if not (ca_dir / "ca.key").exists():
                run(
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "365",
                    "-sha256",
                    "-subj",
                    "/CN=claude-3p-on-bedrock test CA",
                    "-keyout",
                    "ca.key",
                    "-out",
                    "ca.pem",
                    "-addext",
                    "basicConstraints=critical,CA:TRUE",
                    "-addext",
                    "keyUsage=critical,keyCertSign,cRLSign",
                )
            run(
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-subj",
                f"/CN={host}",
                "-keyout",
                "server.key",
                "-out",
                "server.csr",
            )
            (ca_dir / "server.ext").write_text(
                f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
            )
            run(
                "openssl",
                "x509",
                "-req",
                "-in",
                "server.csr",
                "-CA",
                "ca.pem",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-days",
                "90",
                "-sha256",
                "-extfile",
                "server.ext",
                "-out",
                "server.pem",
            )
        finally:
            os.umask(old)
        self.cfg["certificate_arn"] = self.aws.client("acm").import_certificate(
            Certificate=(ca_dir / "server.pem").read_bytes(),
            PrivateKey=(ca_dir / "server.key").read_bytes(),
            CertificateChain=(ca_dir / "ca.pem").read_bytes(),
            Tags=[{"Key": "purpose", "Value": f"{USE_CASE}-test"}],
        )["CertificateArn"]
        self.cfg["certificate_private_ca"] = True
        print(
            f"  imported {self.cfg['certificate_arn']}; CA certificate for laptops: {ca_dir / 'ca.pem'}"
        )

    # 3 ─────────────────────────────────────────────────────────────────────
    def step_entra(self) -> None:
        header(
            "3. Microsoft Entra ID (docs/walkthrough.md: Register the Entra application)"
        )
        guid = re.compile(gc._GUID)
        old = self.default_for("oidc", {}) or {}
        tenant = self.ui.ask(
            "Directory (tenant) ID (Entra admin center > Overview)",
            self.answers.get("tenant_id"),
            lambda v: None if guid.match(v) else "not a GUID",
        )
        client = self.ui.ask(
            "Application (client) ID of the gateway app (App registrations > Overview)",
            self.answers.get("client_id"),
            lambda v: None if guid.match(v) else "not a GUID",
        )
        domains = self.ui.ask_list(
            "Email domains allowed to sign in (e.g. corp.example.com)",
            self.answers.get("email_domains"),
            lambda xs: None if xs else "at least one domain",
        )
        oidc: dict[str, Any] = {
            "tenant_id": tenant,
            "client_id": client,
            "client_secret_name": self.answers.get("secret_name")
            or old.get("client_secret_name")
            or "claude-3p/entra-oidc",
            "allowed_email_domains": domains,
        }
        if self.ui.confirm(
            "Fall back to preferred_username when users have no mail attribute (common in test tenants)?",
            bool(old.get("email_claim")),
        ):
            oidc["email_claim"] = ["email", "preferred_username"]
        # Sign-in restrictions the wizard doesn't ask about (allowed_roles,
        # groups_claim, extra_auth_params, ...) must survive a re-run.
        for key, value in (self.existing.get("oidc") or {}).items():
            if key != "email_claim":  # answered above
                oidc.setdefault(key, value)
        self.cfg["oidc"] = oidc
        redirect = f"https://{self.cfg['hostname']}/oauth/callback"
        print(
            "  On the app registration, check (docs/walkthrough.md, Register the Entra application):"
        )
        for line in (
            f"Authentication > Web redirect URI: {redirect}",
            "Certificates & secrets: a client secret whose value you have",
            "API permissions: Graph delegated openid, profile, email, offline_access + Grant admin consent",
            "Token configuration: optional claim email on the ID token",
            "App roles: one per team (Users/Groups); Enterprise app: Assignment required = Yes",
        ):
            print(f"    - {line}")
        self.app_roles = self._az_inspect(tenant, client, redirect)

    def _az_inspect(self, tenant: str, client: str, redirect: str) -> list[str]:
        if not shutil.which("az"):
            print("  (Azure CLI not installed: the checks above are manual)")
            return []

        def az(*args):
            r = subprocess.run(  # nosec B603 - fixed argv, read-only az queries
                ["az", *args, "-o", "json"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,  # a non-zero exit is handled: the check is skipped
            )
            return (
                json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
            )

        acct = az("account", "show")
        if not acct or acct.get("tenantId") != tenant:
            print(
                f"  (az is not signed in to tenant {tenant}: `az login --tenant {tenant} --allow-no-subscriptions` to auto-check)"
            )
            return []
        app = az("ad", "app", "show", "--id", client)
        if not app:
            print(
                "  ! az could not read the app registration (wrong client ID or no directory read rights)"
            )
            return []
        sp = az("ad", "sp", "show", "--id", client) or {}
        roles = [
            r["value"]
            for r in app.get("appRoles", [])
            if r.get("isEnabled") and r.get("value")
        ]
        claims = [
            c["name"] for c in (app.get("optionalClaims") or {}).get("idToken") or []
        ]
        checks = [
            (
                "Web redirect URI",
                redirect in (app.get("web") or {}).get("redirectUris", []),
            ),
            ("optional ID-token claim email", "email" in claims),
            (f"App roles defined ({', '.join(roles) or 'none'})", bool(roles)),
            ("Assignment required", bool(sp.get("appRoleAssignmentRequired"))),
        ]
        print("  Read from Entra with az (read-only):")
        for name, ok in checks:
            print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
        return roles

    # 4 ─────────────────────────────────────────────────────────────────────
    def step_secret(self) -> None:
        header("4. Entra client secret in Secrets Manager")
        name = self.cfg["oidc"]["client_secret_name"]
        sm = self.aws.client("secretsmanager")
        try:
            meta = sm.describe_secret(SecretId=name)
            if meta.get("DeletedDate"):
                raise SystemExit(
                    f"FAIL: secret {name} is scheduled for deletion; restore it or pick another name"
                )
            print(f"  {name} exists (value not read)")
            if not self.ui.confirm(
                "Replace its value (for example after rotating the secret in Entra)?",
                False,
            ):
                return
            value = getpass.getpass("  New client secret value (hidden): ")
            if value and self.ui.confirm(f"Store the new value in {name}?", True):
                sm.put_secret_value(SecretId=name, SecretString=value)
                print(
                    "  stored (running gateway tasks pick it up on their next restart)"
                )
            return
        except sm.exceptions.ResourceNotFoundException:
            pass
        value = getpass.getpass(
            f"  Client secret VALUE of app {self.cfg['oidc']['client_id']} (hidden): "
        )
        if not value:
            raise SystemExit("stopped: no secret value entered")
        if not self.ui.confirm(f"Create secret {name} in {self.aws.region}?", True):
            raise SystemExit("stopped: secret not stored")
        sm.create_secret(
            Name=name,
            SecretString=value,
            Description=f"Entra client secret for {USE_CASE}",
            Tags=[{"Key": "purpose", "Value": USE_CASE}],
        )
        print(f"  created {name}")

    # 5 ─────────────────────────────────────────────────────────────────────
    def step_models(self) -> None:
        header(f"5. Model catalog (Anthropic profiles ACTIVE in {self.aws.region})")
        profiles = self.aws.anthropic_profiles()
        prev = self.answers.get("models") or [
            m["bedrock"] for m in self.existing.get("models", [])
        ]
        for i, p in enumerate(profiles, 1):
            print(f"    {i:2}. {p}{'   *' if p in prev else ''}")
        print(
            "  eu./us./apac. keep inference in that geography; global. routes worldwide."
        )
        default = (
            ",".join(str(profiles.index(p) + 1) for p in prev if p in profiles) or None
        )
        while True:
            raw = self.ui.ask(
                "Numbers of the models to offer (comma-separated)", default
            )
            try:
                picked = [
                    profiles[int(x) - 1] for x in raw.replace(" ", "").split(",") if x
                ]
            except (ValueError, IndexError):
                print("    ! use the numbers shown")
                continue
            ids = [model_id_from_profile(p) for p in picked]
            if picked and len(ids) == len(set(ids)):
                break
            print(
                "    ! pick at least one model, and each model once (not both eu. and global. of the same one)"
            )
        self.cfg["models"] = [
            {"id": i, "label": model_label(i), "bedrock": p}
            for i, p in zip(ids, picked)
        ]
        for m in self.cfg["models"]:
            print(f"    {m['id']:22} {m['label']:20} {m['bedrock']}")
        self.answers["models"] = picked

    # 6 ─────────────────────────────────────────────────────────────────────
    def step_teams(self) -> None:
        header("6. Teams (Entra App Role -> models) and the default policy")
        ids = [m["id"] for m in self.cfg["models"]]
        print(f"  catalog: {', '.join(ids)}")
        if getattr(self, "app_roles", None):
            print(f"  App Roles found in Entra: {', '.join(self.app_roles)}")

        def models_ok(xs):
            bad = [x for x in xs if x not in ids]
            return f"not in the catalog: {bad}" if bad else None

        def parse_models(v: str) -> list[str]:
            v = v.strip()
            return (
                []
                if v.lower() in ("none", "[]")
                else [x.strip() for x in v.split(",") if x.strip()]
            )

        def ask_models(prompt: str, default: list[str]) -> list[str]:
            raw = self.ui.ask(
                f'{prompt} ("none" for no models)',
                ", ".join(default) or "none",
                lambda v: models_ok(parse_models(v)),
            )
            return parse_models(raw)

        prev = self.answers.get("teams") or self.existing.get("teams") or []
        n = int(
            self.ui.ask(
                "How many teams",
                str(len(prev) or 1),
                lambda v: None if v.isdigit() else "a number",
            )
        )
        teams = []
        for i in range(n):
            p = prev[i] if i < len(prev) else {}
            print(f"  team {i + 1}:")
            t = {
                "name": self.ui.ask(
                    "    team label for platform.yaml (lowercase, e.g. engineering; the Entra role comes next)",
                    p.get("name"),
                    lambda v: (
                        None
                        if re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", v)
                        else "lowercase letters, digits, -"
                    ),
                ),
                "entra_roles": self.ui.ask_list(
                    "    Entra App Role value(s), exact spelling as in Entra (e.g. Claude.Engineering)",
                    [r for r in p.get("entra_roles") or [] if valid_role(r)] or None,
                    roles_ok,
                ),
                "models": ask_models("    models", p.get("models", ids)),
            }
            deny = self.ui.ask_list(
                "    denied Claude Code tools (empty for none)",
                p.get("deny_tools") or [],
            )
            if deny:
                t["deny_tools"] = deny
            teams.append(t)

        # Merge in what the wizard doesn't ask about (settings, desktop_settings,
        # start_model, ...) from the existing team with the same name, or the same
        # Entra roles when it was renamed. Never drop a policy silently.
        old_teams = self.existing.get("teams") or []

        def old_for(t: dict) -> dict:
            for o in old_teams:
                if o.get("name") == t["name"]:
                    return o
            for o in old_teams:
                if set(o.get("entra_roles") or []) == set(t["entra_roles"]):
                    return o
            return {}

        merged = [
            {**{k: v for k, v in old_for(t).items() if k not in _WIZARD_TEAM_KEYS}, **t}
            for t in teams
        ]
        kept = {id(o) for t in teams for o in [old_for(t)] if o}
        dropped = [o.get("name", "?") for o in old_teams if id(o) not in kept]
        if dropped:
            print(
                f"  ! These existing teams are not in your answers: {', '.join(dropped)}"
            )
            if not self.ui.confirm(
                "Remove them, with their roles, models and tool rules from platform.yaml?"
            ):
                raise SystemExit(
                    "stopped: platform.yaml unchanged. Re-run and keep those teams."
                )
        self.cfg["teams"] = merged
        pd = self.answers.get("default") or self.existing.get("default") or {}
        print(
            "  default policy (signed in, in no team; its denied tools apply to every team):"
        )
        # An explicit `models: []` is a deny-all policy; only a missing key gets
        # the suggested model.
        default = {"models": ask_models("    models", pd.get("models", ids[-1:]))}
        deny = self.ui.ask_list(
            "    denied tools for everyone",
            pd.get("deny_tools", ["Read(./.env)"]),
        )
        if deny:
            default["deny_tools"] = deny
        self.cfg["default"] = {
            **{
                k: v
                for k, v in (self.existing.get("default") or {}).items()
                if k not in _WIZARD_POLICY_KEYS
            },
            **default,
        }
        self.answers["teams"], self.answers["default"] = teams, default

    # 7 ─────────────────────────────────────────────────────────────────────
    def step_agentcore(self) -> None:
        header("7. AgentCore Gateway tools in Claude Code and Cowork (optional)")
        print(
            "  Adds the platform's AgentCore Gateway (web search and other targets) as an\n"
            "  MCP server. People sign in to it with Entra ID; no proxy, no Cognito.\n"
            "  Needs: the platform on identity.mode: direct with the 'AgentCore Gateway'\n"
            "  Entra app (docs/walkthrough.md: Register the AgentCore Gateway in Entra)."
        )
        prev = self.existing.get("agentcore_tools") or {}
        if not self.ui.confirm(
            "Give Claude Code and Cowork the AgentCore tools?", bool(prev)
        ):
            self.cfg.pop("agentcore_tools", None)
            return
        ident = self.raw.get("identity") or {}
        direct = ident.get("idp") == "entra_id" and ident.get("mode") == "direct"
        if not direct:
            print(
                "  ! platform.yaml identity is not Entra direct mode, so the AgentCore\n"
                "    Gateway won't accept Entra sign-ins. Set identity: idp entra_id,\n"
                "    mode direct, tenant_id, client_id, client_secret_name (walkthrough)."
            )
            if not self.ui.confirm("Add the tools anyway and fix identity later?"):
                self.cfg.pop("agentcore_tools", None)
                return
        block: dict[str, Any] = {
            "client_id": self.ui.ask(
                "Application (client) ID of the 'AgentCore Gateway' Entra app",
                prev.get("client_id") or (ident.get("client_id") if direct else None),
                lambda v: None if re.match(gc._GUID, v) else "a GUID",
            )
        }
        if direct and block["client_id"] != ident.get("client_id"):
            print(
                "  ! differs from identity.client_id; the gateway accepts only that app's tokens"
            )
        names = [t["name"] for t in self.cfg.get("teams", [])]

        def teams_ok(v: str) -> str | None:
            bad = sorted({x.strip() for x in v.split(",") if x.strip()} - set(names))
            return f"not a team: {bad}" if bad else None

        # ask, not ask_list: an empty answer ("everyone") is a valid default.
        teams = [
            x.strip()
            for x in self.ui.ask(
                f"Teams that get the tools (empty for everyone; teams: {', '.join(names) or 'none'})",
                ", ".join(prev.get("teams") or []),
                teams_ok,
            ).split(",")
            if x.strip()
        ]
        if teams:
            block["teams"] = teams
        url = f"/{self.raw.get('project', 'agentcore-workshop')}/{self.raw.get('environment', 'dev')}/gateway/url"
        try:
            found = self.aws.client("ssm").get_parameter(Name=url)["Parameter"]["Value"]
            print(f"  AgentCore Gateway found: {found}")
            print(
                "  In Entra, this URL must be an Application ID URI of that app (manifest)."
            )
        except Exception:  # noqa: BLE001 - not deployed yet is a normal first run
            print(
                f"  AgentCore Gateway not deployed yet ({url} missing). Deploy the platform's\n"
                "  gateway stack too; then add its URL in Entra as described in the walkthrough."
            )
        self.cfg["agentcore_tools"] = block

    # 8 ─────────────────────────────────────────────────────────────────────
    def step_write(self) -> None:
        header("8. Write platform.yaml")
        cfg = {"claude_version": self._claude_version(), **self.cfg}
        # Keep every key the wizard doesn't ask about (spend_admin_roles, guardrail,
        # desktop, policy_mode, ...), including ones added by hand or in later versions.
        for k, v in self.existing.items():
            if k not in _WIZARD_OWNED_KEYS and k not in cfg:
                cfg[k] = v
        while True:  # the same validation as `deploy.sh design`
            try:
                gc.load(cfg)
                break
            except ValueError as e:
                print("  ! The configuration is not valid yet:")
                for line in readable_errors(e):
                    print(f"      - {line}")
                if not self.ui.interactive or not self.ui.confirm(
                    "Go back to the teams and default policy (step 6) to fix it?", True
                ):
                    raise SystemExit(
                        "stopped: platform.yaml unchanged. Re-run the wizard to fix the values above."
                    ) from None
                self.step_teams()
                cfg.update(teams=self.cfg["teams"], default=self.cfg["default"])
        print(render_block(cfg))
        if not self.ui.confirm(
            f"Write this block into {self.manifest} (a backup goes to {BACKUP_DIR})?",
            True,
        ):
            raise SystemExit("stopped: platform.yaml unchanged")
        self._write_if_changed(upsert_use_case_block(self.text, cfg), "use-case block")
        self._save_answers()

    def _claude_version(self) -> str:
        current = self.default_for("claude_version")
        try:
            with urllib.request.urlopen(STABLE_URL, timeout=5) as r:  # nosec B310 - fixed https URL
                stable = r.read().decode().strip()
        except Exception:  # noqa: BLE001 - offline: keep the current pin
            stable = None
        return self.ui.ask(
            f"Claude Code version for the gateway (stable today: {stable or 'unknown'})",
            current or stable,
            lambda v: None if re.match(r"^\d+\.\d+\.\d+$", v) else "x.y.z",
        )

    def _write_if_changed(self, new: str, what: str) -> None:
        if new == self.text:
            return
        backup = backup_path(self.manifest)
        shutil.copyfile(self.manifest, backup)
        backup.chmod(0o600)
        self.manifest.write_text(new)
        self.text, self.raw = new, yaml.safe_load(new) or {}
        print(f"  platform.yaml updated ({what})")

    def _save_answers(self) -> None:
        ANSWERS_DIR.mkdir(exist_ok=True)
        path = (
            ANSWERS_DIR
            / f"{self.raw.get('project', 'agentcore-workshop')}-{self.raw.get('environment', 'dev')}.answers.yaml"
        )
        # Generic choices only (Region, models, teams). Customer identifiers
        # (tenant, app, domains, hostname, networks, certificate) are never saved
        # and never offered as defaults; add them by hand to replay a run.
        keep = {k: v for k, v in self.answers.items() if k in GENERIC_ANSWER_KEYS}
        ANSWERS_DIR.chmod(0o700)
        path.write_text(yaml.safe_dump(keep, sort_keys=False))
        path.chmod(0o600)
        print(f"  generic answers saved (no identifiers, no secrets): {path}")

    def run(self) -> int:
        self.step_platform()
        self.step_hostname()
        self.step_certificate()
        self.step_entra()
        self.step_secret()
        self.step_models()
        self.step_teams()
        self.step_agentcore()
        self.step_write()
        return check(self.manifest)


# ── check (read-only) ────────────────────────────────────────────────────────


def check(manifest: Path) -> int:
    header("9. Check (read-only)")
    results: list[tuple[str, str]] = []

    def record_result(level, msg):
        results.append((level, msg))
        print(f"  [{level}] {msg}")

    raw = yaml.safe_load(manifest.read_text()) or {}
    block = (raw.get("use_cases") or {}).get(USE_CASE)
    if block is None:
        record_result("FAIL", f"use_cases.{USE_CASE} missing in {manifest}")
        return 1
    try:
        cfg = gc.load(block)
        record_result("PASS", "use-case block valid")
    except ValueError as e:
        record_result("FAIL", f"use-case block invalid: {e}")
        return 1
    try:
        from infra_utils.platform_config import load_platform_config

        os.environ.setdefault("PLATFORM_ALLOW_PLACEHOLDERS", "0")
        load_platform_config(manifest)
        record_result("PASS", "platform.yaml valid (deploy.sh design rules)")
    except Exception as e:  # noqa: BLE001 - any validation error is a FAIL line, not a crash
        record_result("FAIL", f"platform.yaml: {e}")
    region = raw.get("region") or "us-east-1"
    try:
        aws = Aws(region)
    except Exception as e:  # noqa: BLE001 - no or expired credentials: report and stop
        record_result("FAIL", f"AWS credentials: {e}")
        return 1
    pinned = str((raw.get("deployment") or {}).get("platform_account", ""))
    record_result(
        "PASS" if pinned == aws.account else ("WARN" if not pinned else "FAIL"),
        f"account {aws.account}" + (f", pinned {pinned}" if pinned else ", not pinned"),
    )
    record_result(
        "PASS" if (raw.get("security") or {}).get("networking") else "FAIL",
        "security.networking on",
    )
    clash = overlaps_platform_vpc(cfg.client_cidrs)
    record_result(
        "FAIL" if clash else "PASS",
        f"client_cidrs vs platform VPC {PLATFORM_VPC}"
        + (f": overlap {clash}" if clash else ""),
    )
    try:
        cert = aws.client("acm").describe_certificate(
            CertificateArn=cfg.certificate_arn
        )["Certificate"]
        names = [cert.get("DomainName", "")] + cert.get("SubjectAlternativeNames", [])
        record_result(
            "PASS" if cert["Status"] == "ISSUED" else "FAIL",
            f"certificate {cert['Status']} ({cert.get('Type')})",
        )
        record_result(
            "PASS" if cert_covers(cfg.hostname, names) else "FAIL",
            f"certificate covers {cfg.hostname}",
        )
        if cert.get("NotAfter"):
            days = (cert["NotAfter"].timestamp() - time.time()) / 86400
            record_result(
                "PASS" if days > 30 else "WARN",
                f"certificate valid {int(days)} more days"
                + (
                    ""
                    if cert.get("Type") == "AMAZON_ISSUED"
                    else " (imported: no auto-renewal)"
                ),
            )
    except Exception as e:  # noqa: BLE001 - not found or no access: a FAIL line for this check
        record_result("FAIL", f"certificate {cfg.certificate_arn}: {e}")
    try:
        secret_meta = aws.client("secretsmanager").describe_secret(
            SecretId=cfg.oidc.client_secret_name
        )
        current = any(
            "AWSCURRENT" in s
            for s in (secret_meta.get("VersionIdsToStages") or {}).values()
        )
        record_result(
            "PASS" if current and not secret_meta.get("DeletedDate") else "FAIL",
            f"secret {cfg.oidc.client_secret_name} present (value not read)",
        )
    except Exception as e:  # noqa: BLE001 - not found or no access: a FAIL line for this check
        record_result(
            "FAIL", f"secret {cfg.oidc.client_secret_name}: {e.__class__.__name__}"
        )
    try:
        profiles = set(aws.anthropic_profiles())
        missing = [
            m.bedrock
            for m in cfg.models
            if not m.bedrock.startswith("arn:") and m.bedrock not in profiles
        ]
        record_result(
            "FAIL" if missing else "PASS",
            "catalog models ACTIVE in "
            + region
            + (f"; missing {missing}" if missing else ""),
        )
        pm = (raw.get("agents") or {}).get(
            "model_id"
        ) or "us.anthropic.claude-sonnet-4-6"
        record_result(
            "PASS" if pm in profiles else "WARN",
            f"platform agent model {pm}"
            + ("" if pm in profiles else " not in this Region (doctor FAIL)"),
        )
    except Exception as e:  # noqa: BLE001 - listing is advisory: WARN, keep checking
        record_result("WARN", f"Bedrock profiles not listed: {e.__class__.__name__}")
    try:
        aws.client("ec2").describe_vpc_endpoint_services(
            ServiceNames=[f"com.amazonaws.{region}.bedrock-agentcore.gateway"]
        )
        record_result(
            "PASS", "AgentCore Gateway VPC endpoint service offered (networking stack)"
        )
    except Exception:  # noqa: BLE001 - not offered or no access: a FAIL line for this check
        record_result(
            "FAIL", f"AgentCore Gateway VPC endpoint service not offered in {region}"
        )
    if cfg.agentcore_tools:
        ident = raw.get("identity") or {}
        direct = ident.get("idp") == "entra_id" and ident.get("mode") == "direct"
        record_result(
            "PASS" if direct else "FAIL",
            "platform identity is Entra direct mode (AgentCore Gateway accepts Entra tokens)",
        )
        record_result(
            "PASS"
            if ident.get("client_id") == cfg.agentcore_tools.client_id
            else "FAIL",
            "agentcore_tools.client_id equals identity.client_id",
        )
        name = f"/{raw.get('project', 'agentcore-workshop')}/{raw.get('environment', 'dev')}/gateway/url"
        try:
            aws.client("ssm").get_parameter(Name=name)
            record_result("PASS", f"AgentCore Gateway deployed ({name})")
        except Exception:  # noqa: BLE001 - missing parameter is the normal first run: WARN
            record_result(
                "WARN", f"AgentCore Gateway not deployed yet ({name}); deploy it first"
            )
    fails = sum(1 for lvl, _ in results if lvl == "FAIL")
    print(f"\n  {len(results) - fails} ok/warn, {fails} failed.")
    if not fails:
        project, env = (
            raw.get("project", "agentcore-workshop"),
            raw.get("environment", "dev"),
        )
        default_manifest = (REPO / "platform.yaml").resolve()
        env_prefix = (
            ""
            if manifest.resolve() == default_manifest
            else f"PLATFORM_CONFIG={manifest} "
        )
        core = ["networking"] + (["auth", "gateway"] if cfg.agentcore_tools else [])
        stacks = " ".join(
            [f"{project}-{env}-{s}" for s in core] + [f"{project}-{env}-uc-{USE_CASE}"]
        )
        print("  Next (from the repository root):")
        print(f"    {env_prefix}./scripts/deploy.sh design")
        print(f"    {env_prefix}./scripts/deploy.sh doctor")
        print(
            f'    {env_prefix}NON_INTERACTIVE=1 ./scripts/deploy.sh deploy --stack "{stacks}"'
        )
        print(f"    {env_prefix}.venv/bin/python use-cases/{USE_CASE}/verify.py")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", choices=["run", "check"], default="run")
    ap.add_argument(
        "--manifest",
        default=os.environ.get("PLATFORM_CONFIG", str(REPO / "platform.yaml")),
    )
    ap.add_argument("--answers", help="answers file from a previous run (no secrets)")
    ap.add_argument(
        "--non-interactive",
        action="store_true",
        help="take every answer from --answers/defaults",
    )
    ap.add_argument(
        "--yes", action="store_true", help="confirm every change (use with care)"
    )
    ap.add_argument(
        "--debug", action="store_true", help="show full tracebacks on unexpected errors"
    )
    args = ap.parse_args(argv)
    manifest = Path(args.manifest).expanduser()
    if not manifest.exists():
        print(
            f"FAIL: {manifest} not found. Create it first: ./scripts/deploy.sh design --profile greenfield"
        )
        return 1
    if args.command == "check":
        return check(manifest)
    answers = (
        yaml.safe_load(Path(args.answers).expanduser().read_text())
        if args.answers
        else {}
    )
    ui = UI(
        interactive=not args.non_interactive and sys.stdin.isatty(), assume_yes=args.yes
    )
    print(
        f"claude-3p-on-bedrock setup wizard\n  manifest: {manifest}\n  Nothing changes in AWS without your 'y'."
    )
    try:
        return Wizard(manifest, ui, answers or {}).run()
    except KeyboardInterrupt:
        print(
            "\n  stopped (Ctrl-C). Re-run any time: completed steps are skipped or offered as defaults."
        )
        return 130
    except Exception as e:  # a customer gets a message, not a traceback
        if args.debug:
            raise
        print(f"\nFAIL: {e.__class__.__name__}: {e}")
        print(
            "  Nothing more was changed. Re-run the wizard: completed steps are offered as defaults."
        )
        print("  Add --debug to see the full error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
