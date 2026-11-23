"""Text the factory did not write, on its way into a model prompt or a run row.

A CI job log is produced by running the pull request's own code, and a diff hunk, a PR body and a
file at a contributor's head are written by whoever opened the PR. Anyone with a GitHub account
can make that text say anything. Three things happen to it here:

  cap      -- a bound on how much of it reaches a prompt, applied first so the patterns below
              never run over megabytes of hostile input.
  redact   -- credentials are replaced, so a secret printed by a build cannot travel on into a
              prompt, a run row or a PR body the factory writes.
  defang   -- anything shaped like a turn boundary or an instruction block is made inert, so the
              text cannot pose as a system message inside the prompt that quotes it.

This does not make the text safe to obey: the role prompts say to treat it as data, and the edit
allowlists bound what the model can do with it. What this bounds is what it can carry.
"""

import re

# Logs arrive truncated to ~2 MB; a prompt gets at most this much of any single untrusted blob.
MAX_CHARS = 200_000

_SECRETS = (
    # AWS access key ids: the documented prefixes plus 16 uppercase/digit characters.
    (
        re.compile(
            r"\b(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b"
        ),
        "[redacted:aws-key-id]",
    ),
    # AWS secret access keys, which are only recognisable beside their own name.
    (
        re.compile(
            r"(?i)(aws)?_?secret_?(access)?_?key\"?'?\s*[:=]\s*\"?'?[A-Za-z0-9/+=]{40}"
        ),
        "[redacted:aws-secret-key]",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "[redacted:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"), "[redacted:github-pat]"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[redacted:private-key]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*"), "[redacted:bearer-token]"),
    # Signed tokens (JWT): three dot-separated base64url segments starting with a JSON header.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "[redacted:jwt]",
    ),
    # Connection strings carrying a password in the userinfo.
    (
        re.compile(
            r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|https?)://[^\s/@\"']+:[^\s/@\"']+@"
        ),
        r"\1://[redacted:credentials]@",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key)\"?'?\s*[:=]\s*\"?'?[^\s\"']{8,}"
        ),
        r"\1=[redacted]",
    ),
)

# Turn boundaries and instruction blocks across the model families we call. A model that meets
# one of these inside quoted data can read it as the start of a new turn.
# Only STRUCTURAL markers are here. A prose imperative ("ignore all previous instructions") has
# no structure to remove, so rewriting it would be content filtering rather than defanging -- and
# it would mangle this repository's own prompts, tests and docs, which discuss exactly that text.
# Prose stays data, which is what the role prompts already tell the model it is.
_DELIMITERS = re.compile(
    r"<\|[\w/]+\|>"  # <|im_start|>, <|eot_id|>, <|system|>
    r"|\[/?INST\]|<</?SYS>>"  # Llama-family
    r"|</?(?:system|assistant|human|user|instructions?|tool_use|function_calls)>"
    r"|^[ \t]{0,3}#{2,}[ \t]*(?:system|instructions?|new instruction|task)s?\b.*$"
    r"|^[ \t]{0,3}(?:System|Assistant|Human|AI)[ \t]*:",
    re.MULTILINE | re.IGNORECASE,
)


def redact(text: str) -> str:
    """Replace every credential shape with a label naming what was removed."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _inert(match: re.Match) -> str:
    """Keep the words, drop the punctuation that gave the marker its shape.

    Echoing the marker verbatim inside a label would leave the delimiter in the prompt, so the
    structural characters go and a reader still sees what was there.
    """
    words = re.sub(r"[^A-Za-z0-9 ._-]+", " ", match.group(0))
    return "[inert " + " ".join(words.split())[:40] + "]"


def defang(text: str) -> str:
    """Make a turn boundary or an instruction block inert without hiding that it was there."""
    return _DELIMITERS.sub(_inert, text)


def clean(text: str, limit: int = MAX_CHARS, tail: bool = True) -> str:
    """Cap, redact, defang. `tail` keeps the end, which is where a failing log says why."""
    if not text:
        return text
    if len(text) > limit:
        cut = f"\n[{len(text) - limit} characters dropped: over the {limit}-character cap]\n"
        text = cut + text[-limit:] if tail else text[:limit] + cut
    return defang(redact(text))
