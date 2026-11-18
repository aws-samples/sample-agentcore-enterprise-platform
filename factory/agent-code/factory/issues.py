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
DIGEST_TITLE = "Factory digest"  # the one pinned issue: digests, proposals and their decisions
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

    def propose(self, title: str, body: str, labels: list[str]) -> str:
        """Post a proposed issue as a comment on the Factory digest issue. A maintainer's /approve creates it."""
        pid = "P-" + secrets.token_hex(3)
        # Escape < and > so the JSON cannot close the HTML comment early.
        data = (
            json.dumps({"id": pid, "title": title, "body": body, "labels": labels})
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        text = (
            f"**Proposal {pid}:** {title}\n\n{body}\n\n"
            f"Reply `/approve {pid}` (or `/reject {pid}`) on this issue.\n\n"
            f"<!-- factory:proposal {data} -->"
        )
        number = digest_issue(self.repo, self.token, create=True)[0] if self.publish else None
        self._do(
            {"op": "propose", "id": pid, "title": title, "labels": labels, "issue": number},
            "POST",
            f"/issues/{number}/comments",
            {"body": text},
        )
        return pid


def apply_plan(tracker: Tracker, reconcile: list[dict], new: list[dict]) -> None:
    """plan_board's output as issue writes."""
    for r in reconcile:
        n = int(r["task"])
        if r["section"] == "Done":
            tracker.close(n, f"Factory PM: {r['comment']}")
        elif r["section"] == "Needs scoping":
            tracker.label(n, add=[NEEDS_SCOPING], remove=[READY])
            tracker.comment(n, f"Factory PM: needs scoping. {r['comment']}")
        else:
            tracker.comment(n, f"Factory PM: {r['comment']}")
    for t in new:
        t["proposal"] = tracker.propose(
            t["title"], t["body"], [PM, SECTION_LABEL[t["section"]]]
        )


def digest_issue(repo: str, token: str, create: bool) -> tuple[int | None, bool]:
    """(number, created): the open 'Factory digest' issue, created when missing and create is set."""
    for i in open_issues(repo, token):  # shortcut: lists every open issue, fine until the repo has thousands
        if i["title"] == DIGEST_TITLE:
            return i["number"], False
    if not create:
        return None, False
    resp = github.request(
        "POST",
        f"/repos/{repo}/issues",
        token,
        {
            "title": DIGEST_TITLE,
            "body": "Factory digest: a daily summary, proposed issues (/approve or /reject), and "
            "their decisions. Commands are read from comments on this issue.",
        },
    )
    return resp["number"], True


def pin(repo: str, token: str, number: int) -> str:
    """Pin the digest issue with GitHub's GraphQL pinIssue mutation. Returns 'pinned' or the error."""
    node_id = github.request("GET", f"/repos/{repo}/issues/{number}", token)["node_id"]
    resp = github.request(
        "POST",
        "/graphql",
        token,
        {
            "query": "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }",
            "variables": {"id": node_id},
        },
    )
    if resp.get("errors"):
        return "error: " + str(resp["errors"][0].get("message", ""))[:120]
    return "pinned"


def _comments(repo: str, token: str, number: int) -> list[dict]:
    out, page = [], 1
    while True:
        batch = github.request(
            "GET", f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}", token
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
