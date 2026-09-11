#!/usr/bin/env bash
#
# Validate the Kiro power in kiro/agentcore-enterprise-platform against Kiro's
# power format, against Kiro's installer rules, and — the part that matters —
# against this repository's own source.
#
#   bash scripts/check-kiro-power.sh
#
# No AWS calls, no network. Runs in CI on every pull request.
#
# The power's whole value is that a reader can trust what it says without
# checking. A hallucinated flag reads exactly like a real one and only fails in
# front of a user, and a renamed flag is indistinguishable from a hallucinated
# one a month later. So this script pins the power's claims to the tree it ships
# with: every file:line citation resolves and is in bounds, every restatement of
# a profile's module sequence matches PROFILE_MODULES in scripts/deploy.sh, and
# every --flag cited next to one of our scripts exists in that script. Shipping
# the power here rather than in a repository of its own is what makes that
# possible: a rename breaks the build instead of quietly breaking a session.
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

NAME="agentcore-enterprise-platform"
POWER_DIR="kiro/$NAME"
status=0

fail() { echo "  FAIL  $*"; status=1; }
ok() { echo "  ok    $*"; }

if [ ! -f "$POWER_DIR/POWER.md" ]; then
  echo "FAIL: no power at $POWER_DIR/POWER.md"
  exit 1
fi

echo "== Power format: $POWER_DIR/POWER.md =="
python3 - "$POWER_DIR" "$NAME" <<'PY' || status=1
import pathlib
import re
import sys

power_dir, name = pathlib.Path(sys.argv[1]), sys.argv[2]
text = (power_dir / "POWER.md").read_text()
bad = False


def fail(msg):
    global bad
    print(f"  FAIL  {msg}")
    bad = True


m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
if not m:
    fail("no YAML frontmatter block at the top of POWER.md")
    sys.exit(1)
fm = m.group(1)

# Kiro's PowerFrontmatterSchema takes exactly these five. There is no version,
# tags, repository or license field; adding one is an error, not an extension.
keys = re.findall(r"^([A-Za-z][A-Za-z0-9_]*):", fm, re.M)
allowed = ["name", "displayName", "description", "keywords", "author"]
extra = [k for k in keys if k not in allowed]
missing = [k for k in allowed if k not in keys]
if extra:
    fail(f"fields that do not exist in the power format: {extra}")
if missing:
    fail(f"required fields missing: {missing}")
if len(keys) != len(set(keys)):
    fail("duplicate frontmatter keys")
if not extra and not missing:
    print("  ok    exactly the 5 allowed fields")


def scalar(key):
    mm = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
    return mm.group(1).strip().strip('"').strip("'") if mm else None


# Kiro warns when these differ, and the power installs under the frontmatter
# name — so a mismatch means the directory a contributor edits and the power a
# user installs have different names.
if scalar("name") != name:
    fail(f"name ({scalar('name')!r}) must equal the directory name ({name!r})")
else:
    print("  ok    name == directory name")

desc = scalar("description") or ""
if not desc:
    fail("description is empty")
else:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", desc.strip()) if s]
    if len(sentences) > 3:
        fail(f"description is {len(sentences)} sentences; the limit is 3")
    else:
        print(f"  ok    description is {len(sentences)} sentence(s), {len(desc)} chars")

kw_block = re.search(r"^keywords:\s*\n((?:\s*-\s*.+\n)+)", fm, re.M)
if kw_block:
    kws = re.findall(r"-\s*(.+)", kw_block.group(1))
elif scalar("keywords"):
    kws = [k for k in scalar("keywords").strip("[]").split(",") if k.strip()]
else:
    kws = []
kws = [k.strip().strip('"').strip("'") for k in kws]

if not 5 <= len(kws) <= 7:
    fail(f"{len(kws)} keywords; the guidance is 5-7")
else:
    print(f"  ok    {len(kws)} keywords")

# A power that activates on "aws" or "deploy" gets uninstalled. Broad keywords
# are the fastest way there, so they are a failure rather than a style note.
broad = {"aws", "deploy", "test", "agent", "agents", "ai", "cloud", "python",
         "cdk", "security", "bedrock", "mcp", "workshop", "platform"}
offenders = [k for k in kws if k.lower() in broad]
if offenders:
    fail(f"keywords too broad (false-positive activation): {offenders}")

# The steering index and the steering directory must agree in both directions.
# A steering file POWER.md never names is unreachable; a name POWER.md routes to
# that does not exist sends the agent looking for nothing.
on_disk = {f.name for f in (power_dir / "steering").glob("*.md")}
referenced = set(re.findall(r"`([a-z0-9-]+\.md)`", text)) & on_disk
if unreferenced := on_disk - referenced:
    fail(f"steering files never referenced from POWER.md: {sorted(unreferenced)}")
for ref in sorted(re.findall(r"^\| `([a-z0-9-]+\.md)` \|", text, re.M)):
    if ref not in on_disk:
        fail(f"POWER.md's steering index points at a file that does not exist: {ref}")
if not bad:
    print(f"  ok    steering index covers all {len(on_disk)} files, no dangling refs")

sys.exit(1 if bad else 0)
PY

echo
echo "== Installability (Kiro rejects a power containing anything else) =="
# Kiro's validatePowerDirectory throws before install if the directory holds a
# hidden file at ANY depth, a script, an archive, or a credential-shaped name.
# The failure message does not name the offending rule, so a stray .DS_Store
# makes the power simply refuse to install. .DS_Store is gitignored here, which
# keeps the committed tree clean but not a contributor's working copy.
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import sys

power_dir = pathlib.Path(sys.argv[1])
bad = False

# From the extension's DISALLOWED_FILE_PATTERNS. Matched as substrings of the
# filename, lowercased, exactly as Kiro matches them.
DISALLOWED = [
    ".env", ".key", ".pem", ".p12", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", ".secret", "token", "password", "api_key", "apikey", ".npmrc",
    ".aws", ".exe", ".dll", ".com", ".msi", ".scr", ".so", ".dylib", ".bin",
    ".run", ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".vbs", ".vbe",
    ".js", ".jsx", ".ts", ".tsx", ".py", ".rb", ".pl", ".php", ".zip", ".tar",
    ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".db", ".sqlite", ".sqlite3",
    ".sql", "node_modules", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
]

def is_allowed(rel, name, is_dir):
    """Kiro's isAllowedFile, faithfully. Checked BEFORE the disallowed patterns,
    which is why mcp.json is fine despite containing '.js' as a substring."""
    if is_dir:
        return len(rel.parts) == 1 and name in ("steering", ".git")
    if len(rel.parts) == 1 and name in ("POWER.md", "mcp.json"):
        return True
    if rel.parts[0] == "steering":
        return name.lower().endswith(".md")
    return False


for p in sorted(power_dir.rglob("*")):
    rel = p.relative_to(power_dir)
    if is_allowed(rel, p.name, p.is_dir()):
        continue
    if p.name.startswith("."):
        print(f"  FAIL  {rel}: hidden files make the power refuse to install")
        bad = True
        continue
    hit = [d for d in DISALLOWED if d in p.name.lower()]
    if hit:
        print(f"  FAIL  {rel}: filename matches Kiro's disallowed pattern {hit[0]!r}")
        bad = True

# Only steering/ may be a directory, and only .md may live in it. Everything
# else at the top level is dropped on install even when it validates, so a file
# put there is invisible to the agent that is supposed to read it.
for p in sorted(power_dir.iterdir()):
    if p.is_dir():
        if p.name != "steering":
            print(f"  FAIL  {p.name}/: only steering/ is copied on install;"
                  " anything else is silently dropped")
            bad = True
    elif p.name not in ("POWER.md", "mcp.json"):
        print(f"  FAIL  {p.name}: only POWER.md and mcp.json are copied on install")
        bad = True

steering = power_dir / "steering"
if not steering.is_dir():
    print("  FAIL  no steering/ directory")
    bad = True
else:
    for p in sorted(steering.iterdir()):
        if p.is_dir():
            print(f"  FAIL  steering/{p.name}/: the installer copies only files"
                  " directly under steering/, so a subdirectory never arrives")
            bad = True
        elif p.suffix != ".md":
            print(f"  FAIL  steering/{p.name}: only .md is allowed under steering/")
            bad = True

if not bad:
    n = len(list(steering.glob("*.md")))
    print(f"  ok    POWER.md, mcp.json and {n} steering file(s); nothing Kiro would"
          " reject or silently drop")
sys.exit(1 if bad else 0)
PY

echo
echo "== mcp.json =="
python3 - "$POWER_DIR" <<'PY' || status=1
import json
import pathlib
import sys

p = pathlib.Path(sys.argv[1]) / "mcp.json"
if not p.exists():
    print("  skip  the power ships no mcp.json")
    sys.exit(0)
bad = False
try:
    cfg = json.loads(p.read_text())
except json.JSONDecodeError as e:
    print(f"  FAIL  not valid JSON: {e}")
    sys.exit(1)

if set(cfg) != {"mcpServers"}:
    print(f"  FAIL  top-level keys must be exactly ['mcpServers'], got {sorted(cfg)}"
          " (display metadata belongs in POWER.md frontmatter)")
    bad = True
else:
    print("  ok    mcpServers only, no display metadata")

for srv, conf in cfg.get("mcpServers", {}).items():
    if "command" not in conf and "url" not in conf:
        print(f"  FAIL  server {srv!r} has neither 'command' nor 'url'")
        bad = True
        continue
    # Kiro deletes autoApprove/allowedTools from a power's mcp.json on load: a
    # power cannot grant itself auto-approval. Shipping them is inert, and it
    # invites POWER.md to promise a posture the runtime will not honour.
    for dead in ("autoApprove", "allowedTools"):
        if dead in conf:
            print(f"  FAIL  {srv}: {dead!r} is deleted by Kiro on load; remove it"
                  " (approvals are the user's to give)")
            bad = True
    print(f"  ok    {srv}: {conf.get('command', conf.get('url'))}")
sys.exit(1 if bad else 0)
PY

echo
echo "== Runbooks are routed from POWER.md =="
# A runbook the agent cannot find is dead weight, and it fails invisibly: the
# model answers from reference material instead and nobody notices the procedure
# was skipped.
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import sys

power_dir = pathlib.Path(sys.argv[1])
power_md = (power_dir / "POWER.md").read_text()
runbooks = sorted((power_dir / "steering").glob("runbook-*.md"))
if not runbooks:
    print("  skip  no runbooks")
    sys.exit(0)
bad = False
for rb in runbooks:
    if f"`{rb.name}`" not in power_md:
        print(f"  FAIL  POWER.md does not route to `{rb.name}`, so the agent will"
              " not find it")
        bad = True
if not bad:
    print(f"  ok    {len(runbooks)} runbook(s), each routed from POWER.md")
sys.exit(1 if bad else 0)
PY

echo
echo "== Link integrity =="
# troubleshooting.md routes by a symptom index of anchor links, so a broken
# anchor silently costs a reader the fix they came for. GitHub's slug is:
# lowercase, drop everything that is not a word char / space / hyphen, then
# replace EACH space with one hyphen -- runs are not collapsed, which is why an
# em-dash or an ellipsis in a heading yields a double hyphen.
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import re
import sys

power_dir = pathlib.Path(sys.argv[1])


def slug(h):
    h = h.strip().lower().replace("`", "")
    h = re.sub(r"[^\w\s-]", "", h)
    return h.replace(" ", "-")


files = [power_dir / "POWER.md", *sorted(power_dir.glob("steering/*.md"))]
readme = power_dir.parent / "README.md"
if readme.exists():
    files.append(readme)

bad = False
anchors = rels = 0
for f in files:
    text = f.read_text()
    heads = {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", text, re.M)}
    for m in re.finditer(r"\]\((#[^)]+|[A-Za-z0-9_./-]+\.md(?:#[^)]+)?)\)", text):
        target = m.group(1)
        line = text[: m.start()].count("\n") + 1
        if target.startswith("#"):
            anchors += 1
            if target[1:] not in heads:
                print(f"  FAIL  {f}:{line}  dead anchor {target}")
                bad = True
        else:
            rels += 1
            if not (f.parent / target.split("#", 1)[0]).exists():
                print(f"  FAIL  {f}:{line}  link to missing file {target}")
                bad = True
if not bad:
    print(f"  ok    {anchors} anchor(s) and {rels} relative link(s) resolve"
          f" across {len(files)} file(s)")
sys.exit(1 if bad else 0)
PY

echo
echo "== Source citations resolve in this tree =="
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import re
import sys

power_dir = pathlib.Path(sys.argv[1])
root = pathlib.Path(".")
# platform.yaml and workshop.env are created by the deploy wizard, so they are
# cited legitimately without existing in a clean checkout.
USER_CREATED = {"platform.yaml", "workshop.env"}
# Paths this repository has retired. The power names them on purpose: someone on
# an older checkout will report a symptom the current tree cannot produce, and
# "that command was replaced, git pull" is the answer. Each entry is asserted to
# be *absent* below, so a resurrected path fails this check instead of hiding in
# an allowlist.
RETIRED = {
    "scripts/test.py": "replaced by scripts/verify.py / deploy.sh verify",
}
pat = re.compile(
    r"\b((?:scripts|stacks|config|agent-code|docs|tests|tools|infra_utils|dashboard)"
    r"/[A-Za-z0-9_./-]+?\.(?:sh|py|ya?ml|md)"
    r"|app\.py|requirements\.txt|platform\.yaml|workshop\.env)"
    r"(?::(\d+)(?:[-–](\d+))?)?"
)
cites = {}
for f in [power_dir / "POWER.md", *sorted(power_dir.glob("steering/*.md"))]:
    text = f.read_text()
    for m in pat.finditer(text):
        line = text[: m.start()].count("\n") + 1
        cites.setdefault((m.group(1), m.group(2), m.group(3)), []).append(f"{f}:{line}")

bad = False
for (path, lo, hi), where in sorted(cites.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
    if path in USER_CREATED:
        continue
    target = root / path
    if path in RETIRED:
        if target.exists():
            print(f"  FAIL  {path} is listed as retired ({RETIRED[path]}) but exists"
                  f"  (cited at {where[0]})")
            bad = True
        continue
    if not target.exists():
        print(f"  FAIL  {path} does not exist in this repo  (cited at {where[0]})")
        bad = True
        continue
    if lo:
        n = sum(1 for _ in target.open(errors="replace"))
        if int(hi or lo) > n:
            print(f"  FAIL  {path}:{lo}-{hi or lo} is past EOF ({n} lines)"
                  f"  (cited at {where[0]})")
            bad = True
if not bad:
    n = len([k for k in cites if k[0] not in USER_CREATED and k[0] not in RETIRED])
    r = len([k for k in cites if k[0] in RETIRED])
    print(f"  ok    {n} source citation(s) resolve, all line ranges in bounds")
    if r:
        print(f"  ok    {r} reference(s) to retired path(s), still absent: "
              f"{', '.join(sorted({k[0] for k in cites if k[0] in RETIRED}))}")
sys.exit(1 if bad else 0)
PY

echo
echo "== Profile sequences match PROFILE_MODULES =="
# The profile picker is where this power's value concentrates, and the same five
# sequences are restated across many files. Pin them to the source so an
# upstream reordering cannot leave a dozen copies quietly stale.
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import re
import sys

power_dir = pathlib.Path(sys.argv[1])
truth = dict(re.findall(r'PROFILE_MODULES\[([a-z-]+)\]="([^"]+)"',
                        pathlib.Path("scripts/deploy.sh").read_text()))
if not truth:
    print("  FAIL  could not read PROFILE_MODULES from scripts/deploy.sh")
    sys.exit(1)

files = [power_dir / "POWER.md", *sorted(power_dir.glob("steering/*.md"))]
bad, checked = False, 0
for f in files:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        for prof, seq in truth.items():
            if f"`{prof}`" not in line:
                continue
            for s in re.findall(r"(?<![\w`])((?:[3-9ABCE][ ,‑-]+)+[3-9ABCE])(?![\w`])", line):
                got = " ".join(re.findall(r"[3-9ABCE]", s))
                checked += 1
                if got != seq:
                    print(f"  FAIL  {f}:{i}  {prof}: power has {got!r},"
                          f" scripts/deploy.sh has {seq!r}")
                    bad = True
if not bad:
    print(f"  ok    {checked} profile sequence(s) across {len(truth)} profiles"
          " match PROFILE_MODULES")
sys.exit(1 if bad else 0)
PY

echo
echo "== Cited flags exist in the script they are used with =="
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import re
import sys

power_dir = pathlib.Path(sys.argv[1])
root = pathlib.Path(".")
files = [power_dir / "POWER.md", *sorted(power_dir.glob("steering/*.md"))]
# Never cross a newline: the next line's command owns its own flags.
pat = re.compile(
    r"((?:scripts/[A-Za-z0-9_.-]+\.(?:py|sh))|(?<![\w/])deploy\.sh)"
    r"((?:[ \t]+(?:--?[A-Za-z0-9-]+|\"[^\"\n]*\"|'[^'\n]*'|[A-Za-z0-9_./=<>${}-]+))*)"
)
bad, checked = False, 0
for f in files:
    text = f.read_text()
    for m in pat.finditer(text):
        script = "scripts/deploy.sh" if m.group(1) == "deploy.sh" else m.group(1)
        target = root / script
        if not target.exists():
            continue
        src = target.read_text()
        for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", m.group(2)):
            checked += 1
            if flag not in src:
                line = text[: m.start()].count("\n") + 1
                print(f"  FAIL  {f}:{line}  {flag} does not exist in {script}")
                bad = True
if not bad:
    print(f"  ok    {checked} cited flag usage(s) exist in the script they are"
          " used with")
sys.exit(1 if bad else 0)
PY

echo
echo "== Command hygiene =="
python3 - "$POWER_DIR" <<'PY' || status=1
import pathlib
import re
import subprocess
import sys

power_dir = pathlib.Path(sys.argv[1])
files = [power_dir / "POWER.md", *sorted(power_dir.glob("steering/*.md"))]
readme = power_dir.parent / "README.md"
if readme.exists():
    files.append(readme)
bad = False

# 1. Every bash block must parse. <placeholder> is the documentation convention
#    but bash reads it as a redirect, so normalise it first -- otherwise every
#    block with a placeholder is a false positive and the gate gets ignored.
blocks = 0
for f in files:
    text = f.read_text()
    for m in re.finditer(r"```bash\n(.*?)```", text, re.S):
        blocks += 1
        block = re.sub(r"<[A-Za-z0-9_ .|-]+>", "PLACEHOLDER", m.group(1))
        r = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
        if r.returncode:
            line = text[: m.start()].count("\n") + 1
            print(f"  FAIL  {f}:{line}  bash syntax: {r.stderr.strip().splitlines()[-1]}")
            bad = True
if not bad:
    print(f"  ok    {blocks} bash block(s) parse")

# 2. No bare `python`. `source .venv/bin/activate` does not survive between an
#    agent's tool calls, so a bare `python scripts/...` fails with a
#    ModuleNotFoundError that looks nothing like its real cause. This repo's own
#    MODULE_VERIFY table uses .venv/bin/python; so should every command here.
hits = []
for f in files:
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if re.search(r"(?<![\w./-])python (?=scripts/|dashboard/|-m infra_utils)", line):
            hits.append(f"{f}:{i}")
for h in hits:
    print(f"  FAIL  {h}  bare 'python' — use .venv/bin/python"
          " (activation does not persist between an agent's tool calls)")
if hits:
    bad = True
else:
    print("  ok    every Python command uses .venv/bin/python")

sys.exit(1 if bad else 0)
PY

echo
echo "== No account-identifying values =="
# CONTRIBUTING.md: do not commit account IDs, tenant/client IDs, endpoints,
# credentials, or any customer-identifying values. The timings and error strings
# in this power came from real deployments, which is exactly how a concrete
# resource id gets pasted in alongside a measurement.
mapfile -t FILES < <(find "$POWER_DIR" "$POWER_DIR/.." -maxdepth 2 -type f -name '*.md' -o -type f -name '*.json' | sort -u)
if (( ${#FILES[@]} == 0 )); then
  fail "no files to scan"
else
  echo "  ..    scanning ${#FILES[@]} file(s)"

  PATTERN='ghp_|github_pat_|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY'
  if hits=$(grep -nIiE "$PATTERN" "${FILES[@]}" 2>/dev/null); then
    echo "$hits" | sed 's/^/  /'
    fail "credential-shaped string"
  else
    ok "no credential-shaped strings"
  fi

  # 111122223333 is the documentation placeholder CONTRIBUTING.md asks for.
  if hits=$(grep -nIoE '\b[0-9]{12}\b' "${FILES[@]}" 2>/dev/null | grep -v '111122223333'); then
    echo "$hits" | sed 's/^/  /'
    fail "12-digit number that may be a real account id (use 111122223333)"
  else
    ok "no real-looking account ids"
  fi

  # Placeholders in commands are fine; a concrete id from a real run is not.
  RESOURCE_IDS='\b(eni|vpc|subnet|sg|ami|rtb|igw|nat|eipalloc)-[0-9a-f]{8,17}\b'
  RESOURCE_IDS+='|\b[a-z]{2}-[a-z]+-[0-9]_[A-Za-z0-9]{9}\b'
  RESOURCE_IDS+='|\bo-[a-z0-9]{10,32}\b'
  if hits=$(grep -nIoE "$RESOURCE_IDS" "${FILES[@]}" 2>/dev/null | grep -v 'o-example123'); then
    echo "$hits" | sed 's/^/  /'
    fail "concrete resource id from a real account — use a placeholder"
  else
    ok "no concrete resource ids"
  fi
fi

echo
if (( status )); then
  echo "FAILED"
else
  echo "All checks passed."
fi
exit $status
