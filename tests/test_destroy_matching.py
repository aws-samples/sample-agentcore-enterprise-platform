"""Build waiter (Provider framework) and runtime log retention."""

import pytest

from infra_utils import build_trigger_lambda as btl


class _FakeCodeBuild:
    def __init__(self, status):
        self.status = status

    def start_build(self, projectName, environmentVariablesOverride):
        return {"build": {"id": f"{projectName}:1"}}

    def batch_get_builds(self, ids):
        return {"builds": [{"buildStatus": self.status}]}


def _patch(monkeypatch, status):
    monkeypatch.setattr(btl, "_client", lambda: _FakeCodeBuild(status))


def test_on_event_returns_build_id(monkeypatch):
    _patch(monkeypatch, "IN_PROGRESS")
    event = {"RequestType": "Create", "ResourceProperties": {"ProjectName": "p"}}
    assert btl.on_event(event, None) == {"PhysicalResourceId": "p:1"}


def test_is_complete_polls(monkeypatch):
    event = {"RequestType": "Create", "PhysicalResourceId": "p:1"}
    _patch(monkeypatch, "IN_PROGRESS")
    assert btl.is_complete(event, None) == {"IsComplete": False}
    _patch(monkeypatch, "SUCCEEDED")
    assert btl.is_complete(event, None)["IsComplete"] is True
    _patch(monkeypatch, "FAILED")
    with pytest.raises(RuntimeError):
        btl.is_complete(event, None)


def test_delete_is_noop(monkeypatch):
    _patch(monkeypatch, "FAILED")
    sent = []
    monkeypatch.setattr(btl.urllib.request, "urlopen", lambda *a, **k: sent.append(a))
    # Provider framework passes a sanitized ResponseURL: no direct response.
    event = {"RequestType": "Delete", "PhysicalResourceId": "p:1", "ResponseURL": "..."}
    assert btl.on_event(event, None) == {"PhysicalResourceId": "p:1"}
    assert btl.is_complete(event, None) == {"IsComplete": True}
    assert sent == []


def test_legacy_direct_delete_responds_success(monkeypatch):
    sent = []
    monkeypatch.setattr(
        btl.urllib.request, "urlopen", lambda req, timeout: sent.append(req)
    )
    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": "old",
        "ResponseURL": "https://cfn-response.example/presigned",
        "StackId": "stack",
        "RequestId": "req",
        "LogicalResourceId": "BuildTriggerCR",
    }
    assert btl.on_event(event, None) == {"PhysicalResourceId": "old"}
    assert len(sent) == 1
    assert sent[0].get_method() == "PUT"
    assert b'"Status": "SUCCESS"' in sent[0].data


def test_waiter_covers_codebuild_timeout():
    from stacks import runtime_stack

    assert (
        runtime_stack.BUILD_WAIT_TIMEOUT_MINUTES > runtime_stack.BUILD_TIMEOUT_MINUTES
    )


def test_retention_for_days_rounds_up():
    from aws_cdk import aws_logs as logs

    from stacks.runtime_stack import retention_for_days

    assert retention_for_days(30) == logs.RetentionDays.ONE_MONTH
    assert retention_for_days(31) == logs.RetentionDays.TWO_MONTHS
    assert retention_for_days(1) == logs.RetentionDays.ONE_DAY
