"""The workstation sync's own rules, against a fake board (no MCP, no AWS).

cd factory && .venv/bin/python -m pytest sync -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import asana_sync as s

from factory import asana

P = "proj"


class FakeBoard:
    project = P

    def __init__(self):
        self.calls = []
        self._sections = {name: f"s-{i}" for i, name in enumerate(asana.SECTIONS)}
        self._tasks = [{"gid": "900001", "section": asana.READY}]

    def sections(self):
        return dict(self._sections)

    def tasks(self):
        return list(self._tasks)

    def create_task(self, section_gid, name, notes):
        self.calls.append(("create", section_gid, name, notes))
        return "900099"

    def move_task(self, gid, section_gid):
        self.calls.append(("move", gid, section_gid))

    def comment(self, gid, text):
        self.calls.append(("comment", gid, text))

    def complete_task(self, gid):
        self.calls.append(("complete", gid))


def op(kind, i=0, **args):
    return {
        "op_id": f"o{i}",
        "op": kind,
        "project": P,
        "status": "pending",
        "created_at": f"2026-11-20T10:0{i}",
        "args": args,
    }


@pytest.mark.parametrize(
    "o,why",
    [
        (op("move_task", task_gid="900001", section=asana.READY), "only a person"),
        (
            op("move_task", task_gid="900001", section=asana.NEEDS_DESIGN),
            "only a person",
        ),
        (op("create_task", section=asana.READY, name="n", notes=""), "only a person"),
        (
            op("move_task", task_gid="900001", section="Platform Ops"),
            "not a factory section",
        ),
        (op("comment", task_gid="123", text=f"{asana.MARKER} x"), "not in project"),
        (op("comment", task_gid="900001", text="no marker"), "marker"),
        (op("delete_task", task_gid="900001"), "unknown op"),
        (
            {
                **op("comment", task_gid="900001", text=f"{asana.MARKER} x"),
                "project": "other",
            },
            "another project",
        ),
    ],
)
def test_the_sync_refuses_what_the_board_rules_forbid(o, why):
    assert why in s.refuse(o, P, {"900001"})


def test_pending_ops_apply_in_order_and_refusals_do_not_stop_the_rest():
    b = FakeBoard()
    ops = [
        op("comment", 2, task_gid="900001", text=f"{asana.MARKER} done"),
        op("move_task", 1, task_gid="900001", section=asana.READY),  # refused: a gate
        op("move_task", 0, task_gid="900001", section=asana.IN_PROGRESS),
        {
            **op("comment", 3, task_gid="900001", text=f"{asana.MARKER} dry"),
            "status": "dry_run",
        },
        op("create_task", 4, section=asana.DESIGN_REVIEW, name="New", notes="spec"),
        op("comment", 5, task_gid="900099", text=f"{asana.MARKER} on the new task"),
    ]
    out = s.apply_pending(ops, b)
    assert [(r["op_id"], r["status"]) for r in out] == [
        ("o0", "applied"),
        ("o1", "refused"),
        ("o2", "applied"),
        ("o4", "applied"),
        ("o5", "applied"),
    ]
    assert b.calls[0] == ("move", "900001", b._sections[asana.IN_PROGRESS])
    assert b.calls[2][3].endswith(
        asana.MARKER
    )  # a created task always carries the marker


def test_a_claimed_op_is_skipped():
    b = FakeBoard()
    out = s.apply_pending(
        [op("complete_task", task_gid="900001")], b, claim=lambda o: False
    )
    assert out == [] and b.calls == []


def test_moved_by_is_whoever_moved_the_task_into_its_current_section():
    t = {"gid": "1", "section": asana.READY, "moved_by": "creator", "moved_at": "x"}
    stories = [
        {
            "resource_subtype": "section_changed",
            "created_by": {"gid": "me"},
            "created_at": "2026-11-20T10:00:00.000Z",
            "text": 'Me moved this task from "Needs design" to "Design review" in P',
        },
        {
            "resource_subtype": "section_changed",
            "created_by": {"gid": "teammate"},
            "created_at": "2026-11-20T11:00:00.000Z",
            "text": 'Teammate moved this task from "Design review" to "Ready for agent" in P',
        },
        {
            "resource_subtype": "comment_added",
            "created_by": {"gid": "me"},
            "created_at": "2026-11-20T12:00:00.000Z",
            "text": "hi",
        },
    ]
    s.with_stories(t, stories)
    assert t["moved_by"] == "teammate" and t["moved_at"] == "2026-11-20T11:00:00+00:00"
    assert t["comments"] == [
        {"by": "me", "at": "2026-11-20T12:00:00+00:00", "text": "hi"}
    ]


def test_a_task_created_in_place_was_moved_there_by_its_creator():
    t = {"gid": "1", "section": asana.READY, "moved_by": "creator", "moved_at": "t0"}
    s.with_stories(
        t,
        [
            {
                "resource_subtype": "added_to_project",
                "created_by": {"gid": "creator"},
                "text": "added",
            }
        ],
    )
    assert t["moved_by"] == "creator"


# --- the REST board and the Lambda entry point ---------------------------------------------


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_rest_board_pages_and_sends_the_token_only_to_asana():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        assert req.full_url.startswith("https://app.asana.com/api/1.0/")
        assert req.unredirected_hdrs["Authorization"] == "Bearer tok"
        if "offset=" not in req.full_url:
            return _Resp(
                {"data": [{"gid": "1", "name": "A"}], "next_page": {"offset": "o2"}}
            )
        return _Resp({"data": [{"gid": "2", "name": "B"}], "next_page": None})

    b = s.RestBoard("tok", P, opener=opener)
    assert b.sections() == {"A": "1", "B": "2"}
    assert len(seen) == 2


def test_rest_board_create_puts_the_task_in_its_section():
    sent = []

    def opener(req, timeout):
        sent.append((req.get_method(), req.full_url, json.loads(req.data or b"{}")))
        return _Resp({"data": {"gid": "900099"}})

    assert (
        s.RestBoard("tok", P, opener=opener).create_task("s-1", "New", "notes")
        == "900099"
    )
    method, url, body = sent[0]
    assert method == "POST" and url.endswith("/tasks")
    assert body["data"]["memberships"] == [{"project": P, "section": "s-1"}]


class _Ac:
    def __init__(self, token):
        self.token, self.calls = token, []

    def get_workload_access_token_for_user_id(self, **kw):
        self.calls.append(("wat", kw))
        return {"workloadAccessToken": "wat"}

    def get_resource_oauth2_token(self, **kw):
        self.calls.append(("token", kw))
        return (
            {"accessToken": self.token}
            if self.token
            else {"authorizationUrl": "u", "sessionUri": "s"}
        )


def test_the_token_is_asked_for_the_fixed_vault_user_with_user_federation():
    ac = _Ac("tok")
    assert s.asana_token(ac, "wl", "http://localhost/x")["accessToken"] == "tok"
    (_, wat), (_, tok) = ac.calls
    assert wat == {"workloadName": "wl", "userId": s.TOKEN_USER}
    assert tok["oauth2Flow"] == "USER_FEDERATION" and tok["scopes"] == s.SCOPES


def test_without_consent_the_lambda_does_nothing(monkeypatch):
    import types

    fake = types.SimpleNamespace(
        client=lambda name: _Ac(None),
        resource=lambda name: pytest.fail(
            "no table is touched before there is a token"
        ),
    )
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setenv("FACTORY_ASANA_PROJECT", P)
    monkeypatch.setenv("WORKLOAD_NAME", "wl")
    monkeypatch.setenv("RETURN_URL", "http://localhost/x")
    assert s.handler({}, None)["outcome"] == "needs_consent"


def test_task_row_reads_type_and_priority():
    row = s.task_row(
        {
            "gid": "1",
            "name": "n",
            "custom_fields": [
                {"name": "Type", "display_value": "Bug"},
                {"name": "Priority", "display_value": "P1·S2"},
                {"name": "Other", "display_value": "x"},
            ],
        },
        P,
    )
    assert (row["type"], row["priority"]) == ("Bug", "P1·S2")


def test_a_transient_failure_is_retried_and_a_real_one_is_not():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise s.McpError("status: 424 - Vendor API communication failure")
        return "ok"

    assert s.with_retry(flaky, sleep=lambda w: None) == "ok" and len(calls) == 3
    calls.clear()

    def broken():
        calls.append(1)
        raise s.McpError("status: 400 - invalid task")

    with pytest.raises(s.McpError):
        s.with_retry(broken, sleep=lambda w: None)
    assert len(calls) == 1
