"""The planner: one job per task needing work, keyed on the human action (no AWS)."""

import pytest

from factory import asana, config, planner

ME, TEAMMATE = "111111", "222222"


@pytest.fixture(autouse=True)
def asana_mode(monkeypatch):
    monkeypatch.setattr(config, "TRACKER", "asana")
    monkeypatch.setattr(config, "ASANA_USERS", [ME])


def task(gid, section, moved_by=ME, moved_at="2026-11-24T10:00:00+00:00", **kw):
    return {
        "gid": gid,
        "name": f"T{gid}",
        "section": section,
        "completed": False,
        "moved_by": moved_by,
        "moved_at": moved_at,
        "comments": [],
        **kw,
    }


def test_every_qualifying_task_gets_its_own_job_in_parallel():
    tasks = [
        task("1", asana.NEEDS_DESIGN),
        task("2", asana.NEEDS_DESIGN),
        task("3", asana.READY),
        task("4", asana.READY, moved_by=TEAMMATE),
        task("5", asana.BACKLOG),
    ]
    jobs = planner.jobs(tasks, {})
    assert {(j["role"], j["gid"]) for j in jobs} == {
        ("designer", "1"),
        ("designer", "2"),
        ("developer", "3"),
    }
    groups = {planner.message(j)["group"] for j in jobs}
    assert len(groups) == 3  # distinct FIFO groups: nothing waits behind another task


def test_a_task_already_attempted_since_the_human_action_is_not_replanned():
    t = task("1", asana.NEEDS_DESIGN, moved_at="2026-11-24T10:00:00+00:00")
    assert planner.jobs([t], {"designer": {"1": "2026-11-24T10:05:00+00:00"}}) == []
    t["comments"] = [{"by": ME, "at": "2026-11-24T11:00:00+00:00", "text": "try again"}]
    assert (
        planner.jobs([t], {"designer": {"1": "2026-11-24T10:05:00+00:00"}})[0]["role"]
        == "designer"
    )


def test_a_new_comment_in_review_plans_a_revision_and_ours_do_not():
    t = task(
        "7",
        asana.IN_REVIEW,
        comments=[
            {
                "by": ME,
                "at": "2026-11-24T12:00:00+00:00",
                "text": f"{asana.MARKER} PR opened",
            },
            {
                "by": TEAMMATE,
                "at": "2026-11-24T12:01:00+00:00",
                "text": "not allowlisted",
            },
        ],
    )
    assert planner.jobs([t], {"developer": {"7": "2026-11-24T11:00:00+00:00"}}) == []
    t["comments"].append(
        {"by": ME, "at": "2026-11-24T12:30:00+00:00", "text": "rename it"}
    )
    (job,) = planner.jobs([t], {"developer": {"7": "2026-11-24T11:00:00+00:00"}})
    msg = planner.message(job)
    assert msg["body"] == {"role": "developer", "task_gid": "7", "revise": True}
    assert msg["dedupe_key"].endswith("2026-11-24T12:30:00+00:00")


def test_the_same_human_action_always_has_the_same_key():
    t = task("1", asana.READY)
    a = planner.message(planner.jobs([t], {})[0])["dedupe_key"]
    b = planner.message(planner.jobs([t], {})[0])["dedupe_key"]
    assert a == b


def test_a_planned_payload_passes_runtime_validation():
    from factory import server

    assert server.validate({"role": "designer", "task_gid": "12"}) == {
        "id": "design-12"
    }
    assert server.validate({"role": "developer", "task_gid": "12", "revise": True}) == {
        "id": "task-12"
    }
    with pytest.raises(ValueError):
        server.validate({"role": "developer", "task_gid": "12; rm"})
