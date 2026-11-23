"""GitHub Issues as the factory's task source.

Labels carry the board state: factory:ready (the developer may take it), factory:in-progress, factory:backlog,
factory:needs-scoping, and factory:pm on issues the PM opened. A merged PR with `Closes #N` closes its issue.
Writes go through Tracker: with publish off it only records what it would do (the dry run).
"""

import json
import re
import secrets
import urllib.parse

from factory import github

READY, IN_PROGRESS, BACKLOG = "factory:ready", "factory:in-progress", "factory:backlog"
NEEDS_SCOPING, PM = "factory:needs-scoping", "factory:pm"
MARKER = "<!-- factory:pm -->"  # in the body too, so a PM issue is recognised even if its label is removed
DIGEST_TITLE = (
    "Factory digest"  # the one pinned issue: digests, proposals and their decisions
)
DIGEST_LABEL = "factory:digest"
PROPOSAL = re.compile(r"<!-- factory:proposal (\{.*?\}) -->")
RESOLVED = re.compile(r"<!-- factory:resolved (P-[0-9a-f]{6}) (approved|rejected) -->")
SECTION_LABEL = {
    "Ready for agent": READY,
    "Backlog": BACKLOG,
}  # plan_board's sections -> labels


def open_issues(repo: str, token: str, label: str | None = None) -> list[dict]:
    """Open issues (not PRs), oldest first, normalised for the agents."""
    out, page = [], 1
    while True:
        q = {
            "state": "open",
            "sort": "created",
            "direction": "asc",
            "per_page": 100,
            "page": page,
        }
        if label:
            q["labels"] = label
        batch = github.request(
            "GET", f"/repos/{repo}/issues?{urllib.parse.urlencode(q)}", token
        )
        out += [
            {
                "number": i["number"],
                "title": i["title"],
                "body": i.get("body") or "",
                "author": (i.get("user") or {}).get("login", ""),
                "labels": [x["name"] for x in i.get("labels", [])],
                "url": i["html_url"],
                "created_at": i["created_at"],
                "updated_at": i["updated_at"],
            }
            for i in batch
            if "pull_request" not in i
        ]
        if len(batch) < 100:
            return out
        page += 1


class Tracker:
    """Issue writes, or (publish=False) a record of the writes it would make."""

    def __init__(self, repo: str, token: str, publish: bool):
        self.repo, self.token, self.publish = repo, token, publish
        self.ops: list[dict] = []

    def _do(self, op: dict, method: str, path: str, body: dict | None = None) -> dict:
        op["applied"] = self.publish
        self.ops.append(op)
        return (
            github.request(method, f"/repos/{self.repo}{path}", self.token, body)
            if self.publish
            else {}
        )

    def comment(self, number: int, text: str) -> None:
        self._do(
            {"op": "comment", "issue": number, "text": text},
            "POST",
            f"/issues/{number}/comments",
            {"body": text},
        )

    def label(self, number: int, add: list[str] = (), remove: list[str] = ()) -> None:
        if add:
            self._do(
                {"op": "add_labels", "issue": number, "labels": list(add)},
                "POST",
                f"/issues/{number}/labels",
                {"labels": list(add)},
            )
        for name in remove:
            try:
                self._do(
                    {"op": "remove_label", "issue": number, "label": name},
                    "DELETE",
                    f"/issues/{number}/labels/{urllib.parse.quote(name, safe='')}",
                )
            except RuntimeError as e:  # the label was not on the issue
                if "-> 404" not in str(e):
                    raise

    def move(self, number, state: str, comment: str = "") -> None:
        """A developer state as labels, plus an optional comment (asana.Board.move moves a section)."""
        n = int(number)
        if state == "Agent: in progress":
            self.label(n, add=[IN_PROGRESS], remove=[READY])
        elif state == "Needs scoping":
            self.label(n, add=[NEEDS_SCOPING], remove=[READY, IN_PROGRESS])
        if comment:
            self.comment(n, comment)

    def close(self, number: int, text: str) -> None:
        self.comment(number, text)
        self._do(
            {"op": "close", "issue": number},
            "PATCH",
            f"/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
        )

    def create(self, title: str, body: str, labels: list[str]) -> int | None:
        resp = self._do(
            {"op": "create", "title": title, "labels": labels},
            "POST",
            "/issues",
            {"title": title, "body": f"{body}\n\n{MARKER}", "labels": labels},
        )
        return resp.get("number")

    def propose(
        self,
        title: str,
        body: str,
        labels: list[str],
        action: str = "create",
        target: int | None = None,
    ) -> str:
        """Post a proposal as a comment on the Factory digest issue; a maintainer's /approve applies it.

        action: create (a new issue), close (target), or needs_scoping (relabel target off factory:ready).
        """
        pid = "P-" + secrets.token_hex(3)
        # Escape < and > so the JSON cannot close the HTML comment early.
        data = (
            json.dumps(
                {
                    "id": pid,
                    "action": action,
                    "target": target,
                    "title": title,
                    "body": body,
                    "labels": labels,
                }
            )
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        text = (
            f"**Proposal {pid}:** {title}\n\n{body}\n\n"
            f"Reply `/approve {pid}` (or `/reject {pid}`) on this issue.\n\n"
            f"<!-- factory:proposal {data} -->"
        )
        number = (
            digest_issue(self.repo, self.token, create=True)[0]
            if self.publish
            else None
        )
        self._do(
            {
                "op": "propose",
                "id": pid,
                "action": action,
                "target": target,
                "title": title,
                "labels": labels,
                "issue": number,
            },
            "POST",
            f"/issues/{number}/comments",
            {"body": text},
        )
        return pid


def apply_plan(tracker: Tracker, reconcile: list[dict], new: list[dict]) -> None:
    """plan_board's output: comments go straight on the issue; closes, relabels and new issues become proposals."""
    for r in reconcile:
        n = int(r["task"])
        if r["section"] == "Done":
            r["proposal"] = tracker.propose(
                f"Close #{n}", f"Factory PM: {r['comment']}", [], "close", n
            )
        elif r["section"] == "Needs scoping":
            r["proposal"] = tracker.propose(
                f"Mark #{n} as needing scoping",
                f"Factory PM: needs scoping. {r['comment']}",
                [NEEDS_SCOPING],
                "needs_scoping",
                n,
            )
        else:
            tracker.comment(n, f"Factory PM: {r['comment']}")
    for t in new:
        t["proposal"] = tracker.propose(
            t["title"], t["body"], [PM, SECTION_LABEL[t["section"]]]
        )


def apply_proposal(tracker: Tracker, p: dict) -> int | None:
    """What /approve does with one proposal. Returns the new issue number for a create, else None."""
    action, target = (
        p.get("action", "create"),
        p.get("target"),
    )  # older proposals had no action
    if action == "close":
        tracker.close(int(target), p["body"])
        return None
    if action == "needs_scoping":
        tracker.label(int(target), add=[NEEDS_SCOPING], remove=[READY])
        tracker.comment(int(target), p["body"])
        return None
    if action != "create":
        raise ValueError(f"unknown proposal action {action!r}")
    return tracker.create(p["title"], p["body"], p["labels"])


def digest_issue(repo: str, token: str, create: bool) -> tuple[int | None, bool]:
    """(number, created): the one open 'Factory digest' issue. The digest and the PM both come through here.

    Found by the factory:digest label plus the exact title, then by title alone (an issue made before the
    label existed); the oldest wins, so a stray duplicate never takes over. Uses the list API, which is
    consistent right after a write, unlike search. Creating is refused when the App lacks the Issues
    permission: without it the lookup cannot be trusted, and every run would open another issue.
    """
    for label in (DIGEST_LABEL, None):
        for i in open_issues(
            repo, token, label
        ):  # shortcut: lists all open issues, fine until thousands
            if i["title"] == DIGEST_TITLE:
                return i["number"], False
    if not create:
        return None, False
    if any(m.startswith("issues:") for m in github.MISSING_PERMISSIONS):
        raise RuntimeError(
            "GitHub App lacks the Issues permission ("
            + ", ".join(github.MISSING_PERMISSIONS)
            + "): not creating the 'Factory digest' issue"
        )
    resp = github.request(
        "POST",
        f"/repos/{repo}/issues",
        token,
        {
            "title": DIGEST_TITLE,
            "labels": [DIGEST_LABEL],
            "body": "Factory digest: a daily summary, proposed issues (/approve or /reject), and "
            "their decisions. Commands are read from comments on this issue.",
        },
    )
    return resp["number"], True


def pin(repo: str, token: str, number: int) -> str:
    """Pin the digest issue with GitHub's GraphQL pinIssue mutation. Returns 'pinned' or the error."""
    try:
        node_id = github.request("GET", f"/repos/{repo}/issues/{number}", token)[
            "node_id"
        ]
        resp = github.request(
            "POST",
            "/graphql",
            token,
            {
                "query": "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }",
                "variables": {"id": node_id},
            },
        )
    except RuntimeError as e:  # e.g. 403 without the Issues permission: a failed pin must not stop the digest
        return "error: " + str(e)[:160]
    if resp.get("errors"):
        return "error: " + str(resp["errors"][0].get("message", ""))[:120]
    return "pinned"


def _comments(repo: str, token: str, number: int) -> list[dict]:
    out, page = [], 1
    while True:
        batch = github.request(
            "GET",
            f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
            token,
        )
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def pending_proposals(repo: str, token: str, number: int) -> dict[str, dict]:
    """Proposals on the digest issue that no approve or reject has resolved yet."""
    proposals, resolved = {}, set()
    for c in _comments(repo, token, number):
        body = c.get("body") or ""
        for m in PROPOSAL.finditer(body):
            p = json.loads(m.group(1))
            proposals[p["id"]] = p
        resolved.update(m.group(1) for m in RESOLVED.finditer(body))
    return {k: v for k, v in proposals.items() if k not in resolved}
