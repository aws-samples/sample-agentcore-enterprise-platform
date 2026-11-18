"""HMAC gate of the webhook ingress (no AWS). Run: python -m pytest factory/infra/tests -q"""

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas"))
import ingress

SECRET = b"test-secret"
REPO = "aws-samples/sample-agentcore-enterprise-platform"


@pytest.fixture
def sent(monkeypatch):
    sent = []
    monkeypatch.setattr(ingress, "_secret", lambda: SECRET)
    monkeypatch.setattr(ingress, "_first_delivery", lambda d: True)
    monkeypatch.setattr(ingress, "_installation_id", lambda: "")
    monkeypatch.setattr(ingress, "_enqueue", lambda m, group, dedupe_id: sent.append(m))
    return sent


def event(body: bytes, sig: str | None) -> dict:
    headers = {"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": "d-1"}
    if sig is not None:
        headers["X-Hub-Signature-256"] = sig
    return {"headers": headers, "body": body.decode(), "isBase64Encoded": False}


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


BODY = json.dumps(
    {
        "action": "completed",
        "repository": {"full_name": REPO},
        "workflow_run": {
            "id": 42,
            "conclusion": "failure",
            "head_branch": "main",
            "path": ".github/workflows/x.yml",
            "head_repository": {"full_name": REPO},
        },
    }
).encode()


def test_valid_signature_is_queued(sent):
    assert ingress.handler(event(BODY, sign(BODY)), None)["statusCode"] == 202
    assert sent and sent[0]["workflow_run"]["id"] == 42


def test_tampered_byte_is_rejected(sent):
    tampered = BODY.replace(b"42", b"43")
    assert ingress.handler(event(tampered, sign(BODY)), None)["statusCode"] == 401
    assert not sent


def test_missing_or_malformed_header_is_rejected(sent):
    assert ingress.handler(event(BODY, None), None)["statusCode"] == 401
    assert ingress.handler(event(BODY, sign(BODY)[7:]), None)["statusCode"] == 401
    assert not sent


def test_secret_unavailable_fails_closed(sent, monkeypatch):
    def boom():
        raise ValueError("no secret")

    monkeypatch.setattr(ingress, "_secret", boom)
    assert ingress.handler(event(BODY, sign(BODY)), None)["statusCode"] == 500
    assert not sent


def test_other_repo_and_factory_branch_are_dropped(sent):
    other = BODY.replace(REPO.encode(), b"someone/else", 1)
    assert ingress.handler(event(other, sign(other)), None)["statusCode"] == 202
    loop = BODY.replace(b'"main"', b'"factory/ci-fix/1"')
    assert ingress.handler(event(loop, sign(loop)), None)["statusCode"] == 202
    assert not sent


def test_other_installation_is_dropped_once_app_is_configured(sent, monkeypatch):
    monkeypatch.setattr(ingress, "_installation_id", lambda: "777")
    mine = BODY.replace(b'"action"', b'"installation": {"id": 777}, "action"')
    other = BODY.replace(b'"action"', b'"installation": {"id": 999}, "action"')
    assert (
        ingress.handler(event(other, sign(other)), None)["statusCode"] == 202
        and not sent
    )
    assert (
        ingress.handler(event(BODY, sign(BODY)), None)["statusCode"] == 202 and not sent
    )
    assert (
        ingress.handler(event(mine, sign(mine)), None)["statusCode"] == 202
        and len(sent) == 1
    )


def test_unreadable_app_secret_fails_closed(sent, monkeypatch):
    def boom():
        raise ValueError("no secret")

    monkeypatch.setattr(ingress, "_installation_id", boom)
    assert ingress.handler(event(BODY, sign(BODY)), None)["statusCode"] == 500
    assert not sent


def pr_body(login="robobc", draft=False, action="opened", branch="feature/x") -> bytes:
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": REPO},
            "pull_request": {
                "number": 7,
                "draft": draft,
                "user": {"login": login},
                "head": {"sha": "a" * 40, "ref": branch},
                "base": {"repo": {"full_name": REPO}},
            },
        }
    ).encode()


def pr_event(body: bytes) -> dict:
    e = event(body, sign(body))
    e["headers"]["X-GitHub-Event"] = "pull_request"
    return e


def test_pull_request_is_queued_for_review_including_factory_branches(sent):
    for body in (pr_body(), pr_body(branch="factory/ci-fix/1")):
        assert ingress.handler(pr_event(body), None)["statusCode"] == 202
    assert [m["role"] for m in sent] == ["pr_reviewer", "pr_reviewer"]
    assert sent[0]["pull_request"] == {"number": 7, "head_sha": "a" * 40}


def test_dependabot_drafts_and_closed_prs_are_not_reviewed(sent):
    for body in (
        pr_body(login="dependabot[bot]"),
        pr_body(draft=True),
        pr_body(action="closed"),
    ):
        assert ingress.handler(pr_event(body), None)["statusCode"] == 202
    assert not sent


def test_merged_factory_pr_queues_the_merge_handler_only(sent):
    def merged(branch, merged=True):
        b = json.loads(pr_body(action="closed", branch=branch))
        b["pull_request"]["merged"] = merged
        return json.dumps(b).encode()

    for body in (
        merged("factory/task/123"),
        merged("feature/x"),
        merged("factory/task/9", merged=False),
    ):
        assert ingress.handler(pr_event(body), None)["statusCode"] == 202
    assert sent == [
        {"role": "merge_handler", "delivery_id": "d-1", "pull_request": {"number": 7}}
    ]


def comment_event(body: str, login: str = "maintainer", kind: str = "User") -> dict:
    payload = {
        "action": "created",
        "repository": {"full_name": REPO},
        "issue": {"number": 7},
        "comment": {"body": body},
        "sender": {"login": login, "type": kind},
    }
    raw = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": "d-2",
        "X-Hub-Signature-256": sign(raw),
    }
    return {"headers": headers, "body": raw.decode(), "isBase64Encoded": False}


def test_pm_command_is_queued_for_its_issue(sent, monkeypatch):
    monkeypatch.setattr(ingress, "_rate_limited", lambda issue: False)
    ingress.handler(comment_event("/pm what is next?"), None)
    assert sent == [
        {
            "role": "issue_command",
            "delivery_id": "d-2",
            "issue": {"number": 7},
            "actor": {"login": "maintainer"},
            "command": "pm",
            "args": "what is next?",
        }
    ]


def test_bot_and_plain_comments_never_queue(sent, monkeypatch):
    monkeypatch.setattr(ingress, "_rate_limited", lambda issue: False)
    ingress.handler(comment_event("/dev", login="factory[bot]", kind="Bot"), None)
    ingress.handler(comment_event("nice work, thanks"), None)
    ingress.handler(comment_event("/approve P-zzzzzz"), None)  # not a proposal id
    assert sent == []


def test_rate_limited_command_is_not_queued(sent, monkeypatch):
    monkeypatch.setattr(ingress, "_rate_limited", lambda issue: True)
    ingress.handler(comment_event("/dev"), None)
    assert sent == []
