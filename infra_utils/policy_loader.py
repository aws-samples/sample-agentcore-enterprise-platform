"""Loader for control-library security control definitions.

Reads IaC-agnostic control artifacts (valid JSON) from ``control-library/``, injects
per-deployment parameters over ``<<token>>`` sentinels, and returns a ready-to-use policy
document (dict or JSON string).

Sentinel syntax is ``<<param_name>>`` — deliberately not ``${...}`` so it never collides
with IAM/SCP policy variables (``${aws:...}``) or Terraform ``templatefile()`` interpolation.

Usage (CDK stack):

    from infra_utils.policy_loader import load_control, load_control_json

    doc = load_control("resource-policy.memory.in-account-only", {
        "account_id": self.account,
        "memory_arn": memory_arn,
        "org_id": org_id,
    })
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - surfaced clearly at runtime
    raise ImportError(
        "policy_loader requires PyYAML to read control-library/catalog.yaml. "
        "Add 'pyyaml' to requirements.txt and reinstall."
    ) from exc

_TOKEN_RE = re.compile(r"<<\s*([A-Za-z0-9_]+)\s*>>")
_DEFAULT_LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "control-library"


def _library_root(library_root: str | Path | None) -> Path:
    root = Path(library_root) if library_root else _DEFAULT_LIBRARY_ROOT
    if not root.is_dir():
        raise FileNotFoundError(f"control-library root not found: {root}")
    return root


def load_catalog(library_root: str | Path | None = None) -> dict[str, Any]:
    """Parse ``control-library/catalog.yaml`` and return it as a dict."""
    root = _library_root(library_root)
    catalog_path = root / "catalog.yaml"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"catalog.yaml not found in {root}")
    return yaml.safe_load(catalog_path.read_text()) or {}


def _find_control(catalog: dict[str, Any], control_id: str) -> dict[str, Any]:
    for entry in catalog.get("controls", []) or []:
        if entry.get("id") == control_id:
            return entry
    known = ", ".join(
        sorted(e.get("id", "?") for e in catalog.get("controls", []) or [])
    )
    raise KeyError(f"control id '{control_id}' not in catalog. Known ids: {known}")


def _resolve_params(entry: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Merge caller params with catalog defaults and validate required ones are present."""
    resolved = dict(params)
    schema = entry.get("params") or {}
    missing = []
    for name, spec in schema.items():
        spec = spec or {}
        if name in resolved:
            continue
        if "default" in spec:
            resolved[name] = spec["default"]
        elif spec.get("required"):
            missing.append(name)
    if missing:
        raise ValueError(
            f"control '{entry['id']}' missing required params: {', '.join(sorted(missing))}"
        )
    return resolved


def _substitute(node: Any, params: dict[str, Any]) -> Any:
    """Recursively replace ``<<token>>`` sentinels in a parsed JSON structure.

    A string that is exactly one token (``"<<name>>"``) is replaced by the raw param value,
    so list/dict-typed params are supported. Tokens embedded inside a larger string
    (e.g. an ARN) are replaced by ``str(value)``.
    """
    if isinstance(node, dict):
        return {k: _substitute(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, params) for v in node]
    if isinstance(node, str):
        whole = _TOKEN_RE.fullmatch(node.strip())
        if whole:
            name = whole.group(1)
            return params.get(name, node)

        def _repl(match: re.Match) -> str:
            name = match.group(1)
            return str(params[name]) if name in params else match.group(0)

        return _TOKEN_RE.sub(_repl, node)
    return node


def _assert_no_unresolved(doc: Any, control_id: str) -> None:
    leftover = sorted(set(_TOKEN_RE.findall(json.dumps(doc))))
    if leftover:
        raise ValueError(
            f"control '{control_id}' has unresolved tokens: {', '.join(leftover)}. "
            "Pass them in params or add defaults in catalog.yaml."
        )


def load_control(
    control_id: str,
    params: dict[str, Any] | None = None,
    *,
    library_root: str | Path | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a control artifact and return the parameterized policy document as a dict."""
    params = dict(params or {})
    root = _library_root(library_root)
    catalog = catalog if catalog is not None else load_catalog(root)
    entry = _find_control(catalog, control_id)

    artifact_path = root / entry["file"]
    if not artifact_path.is_file():
        raise FileNotFoundError(f"control file not found: {artifact_path}")

    resolved = _resolve_params(entry, params)
    doc = json.loads(artifact_path.read_text())
    rendered = _substitute(doc, resolved)
    _assert_no_unresolved(rendered, control_id)
    return rendered


def load_control_json(
    control_id: str,
    params: dict[str, Any] | None = None,
    *,
    library_root: str | Path | None = None,
    catalog: dict[str, Any] | None = None,
) -> str:
    """Same as :func:`load_control` but returns a compact JSON string."""
    return json.dumps(
        load_control(control_id, params, library_root=library_root, catalog=catalog),
        separators=(",", ":"),
    )
