"""Every per-role setting the factory has, in one table keyed (repo, role).

One row per role, readable in one screen: the models that role calls, its input-token cap,
its dollar cap per run, and the environment variable whose "true" lets it write to GitHub.
A role lists only the fields it actually uses; a field with one value everywhere stays a
constant in the module that uses it, not a column here.

Fail closed. A role with no row gets no models, a zero token cap and publish=False, and the
reason is appended to ERRORS so the run row carries it. There is no path where a missing or
unreadable row results in a live post.

Deploy-time override, no rebuild: FACTORY_<ROLE>_<FIELD> replaces one field, e.g.
FACTORY_CI_FIXER_FIX_MODEL or FACTORY_PR_REVIEWER_TOKEN_CAP. The publish switches
(FACTORY_PUBLISH and the three beside it) are env vars by construction.
"""

import os

REPO = os.environ.get(
    "FACTORY_REPO", "aws-samples/sample-agentcore-enterprise-platform"
)

SONNET = "us.anthropic.claude-sonnet-5-5"
OPUS = "us.anthropic.claude-opus-5-5"
FABLE = "us.anthropic.claude-fable-5-2"
SOL = "us.openai.gpt-6.1-sol"
ASTRA = "us.openai.gpt-6-astra"

# The coders are Claude and the reviewer is OpenAI on purpose: a reviewer that shares the
# coder's family shares its blind spots.
ROLES = {
    "aws-samples/sample-agentcore-enterprise-platform": {
        "ci_fixer": {
            "models": {"triage": SONNET, "fix": OPUS},
            "token_cap": 200_000,
            "publish_env": "FACTORY_PUBLISH",
        },
        "pr_reviewer": {
            "models": {"first_pass": SOL, "deep_pass": ASTRA},
            "token_cap": 250_000,
            "cost_cap_usd": 0.20,  # the whole review, both passes
            "publish_env": "FACTORY_REVIEW_PUBLISH",
        },
        "developer": {
            "models": {"plan": SONNET, "code": FABLE},
            "token_cap": 600_000,
            "publish_env": "FACTORY_DEV_PUBLISH",
        },
        "product_manager": {
            "models": {
                "main": OPUS,
                "backlog": FABLE,
            },  # backlog: initial full-repo build
            "token_cap": 400_000,
            "publish_env": "FACTORY_PM_PUBLISH",
        },
        "merge_handler": {"publish_env": ""},  # reads a merged PR; no model, no write
        "digest": {"publish_env": ""},  # summarises the runs table; no model call
    },
}

# Issue authors the factory acts on. The developer takes an issue, and the PM reconciles or
# comments on one, only when its author is here -- a maintainer's factory:ready label or /dev
# comment does not widen it. Adding a teammate is an edit to this list.
# Not applied to PR reviews, the CI fixer or the merge handler: those judge code, not a request.
AUTHORS = {
    "aws-samples/sample-agentcore-enterprise-platform": ["Robobc"],
}

ERRORS: list[str] = []  # config problems, surfaced on every run row this process writes

# A fail-closed row can call nothing, spend nothing and post nowhere.
_FAIL_CLOSED = {"models": {}, "token_cap": 0, "cost_cap_usd": 0.0, "publish_env": ""}


def _override(role: str, field: str, value):
    """FACTORY_<ROLE>_<FIELD>, cast to the type of the configured value."""
    raw = os.environ.get(f"FACTORY_{role.upper()}_{field.upper()}")
    if raw is None:
        return value
    try:
        return type(value)(raw) if isinstance(value, (int, float)) else raw
    except ValueError:
        ERRORS.append(f"FACTORY_{role.upper()}_{field.upper()}={raw!r} is not a number")
        return value


def role(name: str, repo: str = "") -> dict:
    """The row for (repo, role), env overrides applied. A missing row fails closed and is recorded."""
    repo = repo or REPO
    row = ROLES.get(repo, {}).get(name)
    if row is None:
        ERRORS.append(f"no config row for ({repo}, {name}): publish off, no models")
        return dict(_FAIL_CLOSED)
    out = {**_FAIL_CLOSED, **row}
    out["token_cap"] = _override(name, "token_cap", out["token_cap"])
    out["models"] = {
        k: _override(name, f"{k}_model", v) for k, v in out["models"].items()
    }
    return out


def models(name: str, repo: str = "") -> dict:
    return role(name, repo)["models"]


def token_cap(name: str, repo: str = "") -> int:
    return role(name, repo)["token_cap"]


def publish(name: str, repo: str = "") -> bool:
    """True only when the role has a row naming a switch AND that switch says "true"."""
    env = role(name, repo)["publish_env"]
    return bool(env) and os.environ.get(env) == "true"


def author_allowed(login: str, repo: str = "") -> bool:
    """May the factory act on an issue this login opened?

    Fail closed: a missing or empty list is nobody, so the factory does nothing rather than
    everything. GitHub logins are case-insensitive, so the comparison is too.
    """
    repo = repo or REPO
    allowed = AUTHORS.get(repo) or []
    if not allowed:
        ERRORS.append(f"no author allowlist for {repo}: no issue is acted on")
        return False
    return str(login).lower() in {a.lower() for a in allowed}
