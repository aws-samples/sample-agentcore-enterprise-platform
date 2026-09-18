#!/usr/bin/env python3
"""Use-case scaffolding: `deploy.sh usecase new <name>` and `usecase list`.

    usecase.py new <name> [--owner X] [--summary "..."] [--requires a,b]
                          [--manifest platform.yaml] [--dry-run]
    usecase.py list

`new` copies use-cases/_template/ to use-cases/<name>/ with the {{tokens}}
filled in, then names the use case in platform.yaml's `use_cases:` block so the
next `deploy.sh build` deploys it and `deploy.sh verify` verifies it. The edit
to platform.yaml is textual, not a YAML round-trip: the file carries comments
worth keeping, and PyYAML would drop every one of them.

Exit codes: 0 ok, 1 failure, 2 usage. Pure stdlib + the repo's own loader; no AWS.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.platform_config import (
    USE_CASES_DIR,
    UseCaseManifest,
    discover_use_cases,
    load_platform_config,
)

NAME_RE = re.compile(UseCaseManifest.model_fields["name"].metadata[0].pattern)
TEMPLATE = USE_CASES_DIR / "_template"


def fill(text: str, tokens: dict[str, str]) -> str:
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold(
    name: str, owner: str, summary: str, requires: list[str], root: Path, dry_run: bool
) -> list[Path]:
    """Copy _template/ → root/<name>/ with tokens filled. Returns the files."""
    dest = root / name
    if dest.exists():
        raise SystemExit(
            f"use-cases/{name}/ already exists; pick another name or remove it"
        )
    tokens = {"name": name, "owner": owner, "summary": summary}
    written: list[Path] = []
    for src in sorted(TEMPLATE.iterdir()):
        if src.name.startswith(".") or src.name == "__pycache__":
            continue
        text = fill(src.read_text(), tokens)
        if src.name == "manifest.yaml" and requires:
            text = re.sub(
                r"^requires: \[.*\]$",
                f"requires: [{', '.join(requires)}]",
                text,
                flags=re.MULTILINE,
            )
        target = dest / src.name
        written.append(target)
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            if src.name == "verify.py":
                target.chmod(0o755)
    return written


def enable_in_manifest(name: str, manifest: Path, dry_run: bool) -> str:
    """Add `<name>: {}` under `use_cases:` textually. Returns what was (or would be) done."""
    entry = f"  {name}: {{}}\n"
    if not manifest.exists():
        return f"no {manifest.name} yet — add this when you create it:\n\nuse_cases:\n{entry}"
    text = manifest.read_text()
    if re.search(rf"^\s+{re.escape(name)}:", text, flags=re.MULTILINE):
        return f"{manifest.name} already names {name}"
    m = re.search(r"^use_cases:[ \t]*(\{\}|)[ \t]*(#.*)?$", text, flags=re.MULTILINE)
    following = text[m.end() :].lstrip("\n") if m else ""
    # Only an EMPTY block is rewritten here: `use_cases:` followed by an indented
    # entry is a populated block and takes the append path below.
    if m and (m.group(1) == "{}" or not following.startswith((" ", "\t"))):
        # `use_cases:` or `use_cases: {}` → the entry goes on the next line.
        head = (
            text[: m.start()]
            + "use_cases:"
            + (f"  {m.group(2)}" if m.group(2) else "")
            + "\n"
        )
        new = head + entry + text[m.end() :].lstrip("\n")
    elif re.search(r"^use_cases:", text, flags=re.MULTILINE):
        # A populated block: insert after the last indented line that follows it.
        lines = text.splitlines(keepends=True)
        start = next(i for i, l in enumerate(lines) if l.startswith("use_cases:"))
        end = start + 1
        while end < len(lines) and (
            lines[end].startswith((" ", "\t")) or not lines[end].strip()
        ):
            end += 1
        lines.insert(end, entry)
        new = "".join(lines)
    else:
        new = text.rstrip("\n") + f"\nuse_cases:\n{entry}"
    if not dry_run:
        manifest.write_text(new)
    return f"{'would add' if dry_run else 'added'} `{name}: {{}}` under use_cases: in {manifest.name}"


def cmd_new(a: argparse.Namespace) -> int:
    if not NAME_RE.match(a.name):
        print(
            f"invalid name {a.name!r}: lowercase, digits, hyphens, 3–41 chars, starts with a letter",
            file=sys.stderr,
        )
        return 2
    root = Path(a.use_cases_dir) if a.use_cases_dir else USE_CASES_DIR
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    requires = (
        [r.strip() for r in a.requires.split(",") if r.strip()] if a.requires else []
    )
    files = scaffold(a.name, a.owner, a.summary, requires, root, a.dry_run)
    note = enable_in_manifest(a.name, manifest, a.dry_run)
    if not a.dry_run:
        # Prove the result loads before telling the user it worked.
        found = discover_use_cases(root)
        if a.name not in found:
            return _fail(f"scaffolded but discover_use_cases() does not see {a.name}")
        # The manifest's use_cases validator resolves names against the REAL
        # use-cases/ dir, so this proof only makes sense when that is where we
        # scaffolded (tests point --use-cases-dir elsewhere and check discovery).
        if manifest.exists() and root == USE_CASES_DIR:
            try:
                load_platform_config(manifest)
            except Exception as exc:  # noqa: BLE001 — surface the validator's own message
                return _fail(
                    f"{manifest.name} no longer validates after the edit:\n{exc}"
                )
    verb = "Would create" if a.dry_run else "Created"
    print(f"{verb} use-cases/{a.name}/:")
    for f in files:
        print(f"  {f.relative_to(root.parent) if root.parent in f.parents else f}")
    print(note)
    print("\nNext:")
    print("  ./scripts/deploy.sh design     # the plan now lists uc-" + a.name)
    print("  ./scripts/deploy.sh build      # deploys the platform and the use case")
    print(
        "  ./scripts/deploy.sh verify     # runs use-cases/"
        + a.name
        + "/verify.py last"
    )
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    root = Path(a.use_cases_dir) if a.use_cases_dir else USE_CASES_DIR
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    enabled: set[str] = set()
    if manifest.exists():
        # Textual, not load_platform_config(): listing must work on a manifest
        # that does not validate yet, and must not resolve names against the
        # real use-cases/ dir when tests point elsewhere.
        block = re.search(
            r"^use_cases:\s*\n((?:[ \t]+.*\n?)*)",
            manifest.read_text(),
            flags=re.MULTILINE,
        )
        if block:
            enabled = set(
                re.findall(
                    r"^[ \t]+([a-z][a-z0-9-]*):", block.group(1), flags=re.MULTILINE
                )
            )
    found = discover_use_cases(root)
    if not found:
        print("no use cases under use-cases/ (run: deploy.sh usecase new <name>)")
        return 0
    w = max(len(n) for n in found)
    print(f"{'NAME'.ljust(w)}  ENABLED  OWNER        STACKS")
    for n, m in found.items():
        print(
            f"{n.ljust(w)}  {'yes' if n in enabled else 'no':7}  {m.owner[:12].ljust(12)} {', '.join(m.stacks)}"
        )
        print(f"{' ' * w}  {m.summary}")
    return 0


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usecase", description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--manifest",
        default=str(REPO / "platform.yaml"),
        help="platform.yaml to edit/read",
    )
    p.add_argument("--use-cases-dir", default="", help=argparse.SUPPRESS)  # tests
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="scaffold a use case and enable it")
    n.add_argument("name")
    n.add_argument("--owner", default=getpass.getuser())
    n.add_argument("--summary", default="")
    n.add_argument(
        "--requires", default="", help="comma-separated core suffixes, default gateway"
    )
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(fn=cmd_new)
    ls = sub.add_parser(
        "list", help="discovered use cases and whether the manifest enables them"
    )
    ls.set_defaults(fn=cmd_list)
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.cmd == "new" and not a.summary:
        a.summary = f"{a.name} use case"
    try:
        return a.fn(a)
    except SystemExit as e:  # scaffold() raises with a message
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        return int(e.code or 0)


if __name__ == "__main__":
    sys.exit(main())
