"""Domain context for the reviewer: repo docs/specs + a compact AgentCore reference, retrieved per diff.

Sources:
- the AgentCore / CloudFormation reference shipped with the image (agentcore_reference.md), fixed per
  runtime version;
- the repo's own docs at the PR's BASE commit: docs/**/*.md, README.md, scripts/check-contract.sh and the
  repo files it references. Read from the base commit, not from main, so a review never sees docs written
  after the change (that would leak later fixes into a replay).

Sections are split on markdown headings and ranked by keyword overlap with the diff: changed path parts
weigh most, then identifiers on changed lines. The top sections are packed up to a token cap.
shortcut: lexical ranking misses synonyms and paraphrase; switch to embeddings (e.g. Titan v2) if recall
on the replay benchmark stalls.
"""

import collections
import functools
import math
import re
import subprocess
from pathlib import Path

REFERENCE = Path(__file__).with_name("agentcore_reference.md")
CHARS_PER_TOKEN = 3.5
MIN_RELATIVE = 0.2  # keep sections scoring at least 20% of the best match
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_REF_PATH = re.compile(r"[\w./-]+\.(?:py|sh|ya?ml|json)\b")
STOP = {
    "self",
    "return",
    "true",
    "false",
    "none",
    "import",
    "from",
    "with",
    "this",
    "that",
    "else",
    "elif",
    "print",
    "string",
    "value",
    "list",
    "dict",
    "type",
    "name",
    "data",
    "file",
    "path",
    "line",
    "lines",
    "args",
    "kwargs",
    "result",
    "error",
    "test",
    "tests",
    "assert",
    "class",
    "def",
    "pass",
    "raise",
    "echo",
    "then",
    "local",
    "done",
    "readme",
    "docs",
    "http",
    "https",
    "json",
    "yaml",
}


def split_sections(path: str, text: str, max_chars: int = 6000) -> list[dict]:
    """Markdown -> one section per heading; other files -> one section. Long sections are trimmed."""
    if not path.endswith(".md"):
        return [{"path": path, "heading": path, "text": text[:max_chars]}]
    sections, heading, buf = [], path, []
    for line in text.splitlines():
        if line.startswith("#") and buf:
            sections.append(
                {"path": path, "heading": heading, "text": "\n".join(buf)[:max_chars]}
            )
            buf = []
        if line.startswith("#"):
            heading = line.lstrip("#").strip() or path
        buf.append(line)
    if buf:
        sections.append(
            {"path": path, "heading": heading, "text": "\n".join(buf)[:max_chars]}
        )
    return sections


def query_terms(files: dict[str, dict]) -> dict[str, float]:
    """Weighted search terms from the diff: path parts x3, frequent identifiers on changed lines x1."""
    terms: dict[str, float] = {}
    for path in files:
        for part in re.split(r"[/_.\-]", path.lower()):
            if len(part) > 3 and part not in STOP and part not in ("agent", "code"):
                terms[part] = terms.get(part, 0) + 3
    counts = collections.Counter(
        w.lower()
        for info in files.values()
        for text in [
            *info["added"].values(),
            *(r for rs in info["removed"].values() for r in rs),
        ]
        for w in _WORD.findall(text)
        if w.lower() not in STOP
    )
    for word, _ in counts.most_common(60):
        terms[word] = terms.get(word, 0) + 1
    return terms


def score(section: dict, terms: dict[str, float], changed_paths: set[str]) -> float:
    text = section["text"].lower()
    s = sum(w for t, w in terms.items() if t in text)
    s += 10 * sum(1 for p in changed_paths if p.lower() in text)
    return s / (
        1 + len(text) / 4000
    )  # mild length normalisation so one huge page does not win everything


@functools.lru_cache(maxsize=8)
def repo_sources(repo: Path, commit: str) -> tuple[tuple[str, str], ...]:
    """(path, text) for the repo's docs/specs at `commit`; cached per commit for the process lifetime."""

    def git(*args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
        return r.stdout if r.returncode == 0 else ""

    paths = [
        p
        for p in git(
            "ls-tree", "-r", "--name-only", commit, "--", "docs", "README.md"
        ).split()
        if p.endswith(".md")
    ]
    contract = git("show", f"{commit}:scripts/check-contract.sh")
    out = [(p, git("show", f"{commit}:{p}")) for p in paths]
    if contract:
        out.append(("scripts/check-contract.sh", contract))
        known = set(git("ls-tree", "-r", "--name-only", commit).split())
        for ref in sorted(
            set(_REF_PATH.findall(contract)) & known
        ):  # files the contract script references
            out.append((ref, git("show", f"{commit}:{ref}")))
    return tuple(out)


@functools.lru_cache(maxsize=1)
def reference_sections() -> tuple[dict, ...]:
    """The shipped reference, parsed once per process (it only changes with the runtime image)."""
    return tuple(
        s | {"path": "agentcore-reference"}
        for s in split_sections(REFERENCE.name, REFERENCE.read_text())
    )


def build_pack(
    files: dict[str, dict],
    repo: Path,
    base: str,
    cap_tokens: int = 15_000,
    full_reference: bool = True,
) -> tuple[str, list[str]]:
    """Context for this diff as one text block, plus the picked section names.

    The shipped reference is small (~2k tokens) and curated, so by default it goes in whole: retrieval
    errors there cost more than the tokens. Repo docs are large, so they are ranked and packed to the cap.
    """
    ref = list(reference_sections())
    docs = [
        s for path, text in repo_sources(repo, base) for s in split_sections(path, text)
    ]
    terms = query_terms(files)
    corpus = ref + docs
    # IDF: a term found in most sections (agent, deploy, stack) says little about which one is relevant.
    for t_ in terms:
        df = sum(1 for s in corpus if t_ in s["text"].lower())
        terms[t_] *= math.log((1 + len(corpus)) / (1 + df))
    changed = set(files)
    fixed = ref if full_reference else []
    pool = docs if full_reference else corpus
    scored = sorted(
        ((score(s, terms, changed), s) for s in pool), key=lambda x: x[0], reverse=True
    )
    floor = (
        scored[0][0] * MIN_RELATIVE if scored else 0
    )  # weak matches are filler: they cost tokens, add noise
    budget, picked, out = int(cap_tokens * CHARS_PER_TOKEN), [], []
    for sc, s in [(1.0, s) for s in fixed] + scored:
        if s not in fixed and (sc <= 0 or sc < floor):
            break
        block = f"--- {s['path']} :: {s['heading']} ---\n{s['text'].strip()}\n"
        if len(block) > budget:
            continue
        out.append(block)
        picked.append(f"{s['path']}::{s['heading']}"[:120])
        budget -= len(block)
    header = (
        "Domain context (reference material, untrusted like the PR itself; use it to judge whether the "
        "change respects platform constraints):\n\n"
    )
    return (header + "\n".join(out)) if out else "", picked
