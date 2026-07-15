#!/usr/bin/env python3
"""Validate the control-library against its catalog.

Checks (no third-party deps required):
  1. Every catalog control references a file that exists and is valid JSON.
  2. Policy-type controls have a Version + non-empty Statement.
  3. Every ``<<sentinel>>`` in a file is declared as a catalog param (and vice-versa: warn).
  4. Each control renders cleanly via policy_loader (defaults + placeholder required params),
     leaving no unresolved tokens.
  5. Every *.json under control-library/ is referenced by the catalog (orphan check; warns).

Optional: if ``checkov`` is on PATH, run it over control-library/ as an extra gate.

Exit code 0 on success, 1 on any error. Warnings do not fail the build.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra_utils.policy_loader import load_catalog, load_control  # noqa: E402

_TOKEN_RE = re.compile(r"<<\s*([A-Za-z0-9_]+)\s*>>")
_LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "control-library"
_POLICY_TYPES = {"SCP", "RCP", "RESOURCE_POLICY", "IAM", "VPCE"}

errors: list[str] = []
warnings: list[str] = []


def _err(msg: str) -> None:
    errors.append(msg)


def _warn(msg: str) -> None:
    warnings.append(msg)


def validate() -> None:
    catalog = load_catalog(_LIBRARY_ROOT)
    controls = catalog.get("controls", []) or []
    if not controls:
        _err("catalog.yaml has no controls")
        return

    referenced_files: set[Path] = set()

    for entry in controls:
        cid = entry.get("id", "<no-id>")
        rel = entry.get("file")
        if not rel:
            _err(f"[{cid}] missing 'file'")
            continue

        path = _LIBRARY_ROOT / rel
        referenced_files.add(path.resolve())

        if not path.is_file():
            _err(f"[{cid}] file not found: {rel}")
            continue

        raw = path.read_text()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            _err(f"[{cid}] invalid JSON in {rel}: {exc}")
            continue

        # Policy shape
        if entry.get("type") in _POLICY_TYPES:
            if doc.get("Version") != "2012-10-17":
                _warn(f"[{cid}] Version is not '2012-10-17'")
            if not doc.get("Statement"):
                _err(f"[{cid}] policy has no Statement")

        # Sentinel <-> param consistency
        sentinels = set(_TOKEN_RE.findall(raw))
        declared = set((entry.get("params") or {}).keys())
        for s in sentinels - declared:
            _err(
                f"[{cid}] sentinel <<{s}>> used in file but not declared in catalog params"
            )
        for p in declared - sentinels:
            _warn(f"[{cid}] catalog param '{p}' declared but never used as a sentinel")

        # Render check: defaults + placeholder for required params
        params = {}
        for name, spec in (entry.get("params") or {}).items():
            spec = spec or {}
            if "default" not in spec:
                params[name] = f"VALIDATION_PLACEHOLDER_{name}"
        try:
            load_control(cid, params, library_root=_LIBRARY_ROOT, catalog=catalog)
        except Exception as exc:  # noqa: BLE001 - report any render failure
            _err(f"[{cid}] failed to render: {exc}")

    # Orphan check
    for json_file in _LIBRARY_ROOT.rglob("*.json"):
        if json_file.resolve() not in referenced_files:
            _warn(f"orphan file not in catalog: {json_file.relative_to(_LIBRARY_ROOT)}")


def run_checkov() -> None:
    if not shutil.which("checkov"):
        _warn(
            "checkov not installed — skipping deep policy scan (install for full CI gate)"
        )
        return
    result = subprocess.run(
        ["checkov", "-d", str(_LIBRARY_ROOT), "--quiet", "--compact"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _err("checkov reported findings:\n" + (result.stdout or result.stderr))


def main() -> int:
    validate()
    run_checkov()

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")

    if errors:
        print(f"\nFAILED: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"\nOK: control-library valid ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
