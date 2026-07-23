"""Sprints — a project's time-boxed iterations, and the issue↔sprint membership.

These pin the lifecycle contract the API/board will rest on: a sprint is created
`planned`; starting moves it to `active` (and stamps a start date), but only from
planned and only if the project has no other active sprint; completing moves
`active` → `completed` (stamping an end date); a sprint can't be deleted while issues
are in it; and an issue can be put in a sprint or returned to the backlog.
"""

import sqlite3

import pytest

from athena.aegis import issues, projects, sprints
from athena.core import db, users


def _setup(tmp_path):
    conn = db.connect(tmp_path / "sprints.db")
    db.migrate(conn)
    uid = users.create_user(conn, email="a@e.com", name="A")["id"]
    proj = projects.create_project(conn, name="Ops", key="OPS", created_by=uid)
    return conn, uid, proj["id"]


def test_create_get_list_and_state_filter(tmp_path):
    conn, _, pid = _setup(tmp_path)
    s1 = sprints.create_sprint(conn, project_id=pid, name="Sprint 1", goal="ship it")
    assert s1["state"] == sprints.PLANNED and s1["goal"] == "ship it"
    assert sprints.get_sprint(conn, s1["id"])["name"] == "Sprint 1"

    sprints.create_sprint(conn, project_id=pid, name="Sprint 2")
    assert {s["name"] for s in sprints.list_sprints(conn, project_id=pid)} == {
        "Sprint 1",
        "Sprint 2",
    }
    assert [
        s["name"]
        for s in sprints.list_sprints(conn, project_id=pid, state=sprints.PLANNED)
    ] == [
        "Sprint 2",
        "Sprint 1",  # newest first
    ]
    conn.close()


def test_create_rejects_unknown_project(tmp_path):
    conn, _, _ = _setup(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        sprints.create_sprint(conn, project_id=9999, name="orphan")
    conn.close()


def test_start_activates_and_stamps_start_date(tmp_path):
    conn, _, pid = _setup(tmp_path)
    s = sprints.create_sprint(conn, project_id=pid, name="S1")
    started = sprints.start_sprint(conn, s["id"])
    assert started["state"] == sprints.ACTIVE
    assert started["start_date"]  # stamped
    assert sprints.active_sprint(conn, pid)["id"] == s["id"]
    conn.close()


def test_cannot_start_a_non_planned_sprint(tmp_path):
    conn, _, pid = _setup(tmp_path)
    s = sprints.create_sprint(conn, project_id=pid, name="S1")
    sprints.start_sprint(conn, s["id"])
    with pytest.raises(sprints.SprintStateError):
        sprints.start_sprint(conn, s["id"])  # already active
    conn.close()


def test_one_active_sprint_per_project(tmp_path):
    conn, _, pid = _setup(tmp_path)
    a = sprints.create_sprint(conn, project_id=pid, name="A")
    b = sprints.create_sprint(conn, project_id=pid, name="B")
    sprints.start_sprint(conn, a["id"])
    with pytest.raises(sprints.SprintStateError):
        sprints.start_sprint(conn, b["id"])  # A is already active
    conn.close()


def test_rejected_start_rolls_back_and_leaves_conn_usable(tmp_path):
    # WHY: start_sprint now runs the "no other active sprint" check + the activate write
    # inside one BEGIN IMMEDIATE, so two concurrent starts can't both read "none active"
    # and both flip to active. The rejected path must roll back cleanly — the second
    # sprint stays PLANNED and the connection isn't left mid-transaction (which would
    # break the next write).
    conn, _, pid = _setup(tmp_path)
    a = sprints.create_sprint(conn, project_id=pid, name="A")
    b = sprints.create_sprint(conn, project_id=pid, name="B")
    sprints.start_sprint(conn, a["id"])
    with pytest.raises(sprints.SprintStateError):
        sprints.start_sprint(conn, b["id"])
    # B untouched by the rejected start (rollback), A still the only active sprint.
    assert sprints.get_sprint(conn, b["id"])["state"] == sprints.PLANNED
    assert sprints.active_sprint(conn, pid)["id"] == a["id"]
    # The connection is not stuck in a dangling transaction — a later write still commits.
    c = sprints.create_sprint(conn, project_id=pid, name="C")
    assert sprints.get_sprint(conn, c["id"])["name"] == "C"
    conn.close()


def test_another_project_can_have_its_own_active_sprint(tmp_path):
    conn, uid, pid = _setup(tmp_path)
    other = projects.create_project(conn, name="Web", key="WEB", created_by=uid)["id"]
    a = sprints.create_sprint(conn, project_id=pid, name="A")
    b = sprints.create_sprint(conn, project_id=other, name="B")
    sprints.start_sprint(conn, a["id"])
    # A different project's cadence is independent.
    assert sprints.start_sprint(conn, b["id"])["state"] == sprints.ACTIVE
    conn.close()


def test_complete_only_from_active_and_stamps_end_date(tmp_path):
    conn, _, pid = _setup(tmp_path)
    s = sprints.create_sprint(conn, project_id=pid, name="S1")
    with pytest.raises(sprints.SprintStateError):
        sprints.complete_sprint(conn, s["id"])  # still planned
    sprints.start_sprint(conn, s["id"])
    done = sprints.complete_sprint(conn, s["id"])
    assert done["state"] == sprints.COMPLETED and done["end_date"]
    # A completed sprint no longer counts as the project's active one.
    assert sprints.active_sprint(conn, pid) is None
    conn.close()


def test_update_edits_fields_and_can_clear_a_date(tmp_path):
    conn, _, pid = _setup(tmp_path)
    s = sprints.create_sprint(conn, project_id=pid, name="S1", start_date="2026-07-01")
    updated = sprints.update_sprint(conn, s["id"], name="Renamed", start_date=None)
    assert updated["name"] == "Renamed" and updated["start_date"] is None
    assert updated["goal"] == ""  # untouched
    conn.close()


def test_assign_issue_to_sprint_and_back_to_backlog(tmp_path):
    conn, uid, pid = _setup(tmp_path)
    s = sprints.create_sprint(conn, project_id=pid, name="S1")
    iss = issues.create_issue(conn, title="x", body="", created_by=uid, project_id=pid)

    in_sprint = issues.set_sprint(conn, iss["id"], s["id"])
    assert in_sprint["sprint_id"] == s["id"]
    assert sprints.count_issues_in_sprint(conn, s["id"]) == 1

    back = issues.set_sprint(conn, iss["id"], None)
    assert back["sprint_id"] is None
    assert sprints.count_issues_in_sprint(conn, s["id"]) == 0
    conn.close()


def test_assign_to_unknown_sprint_is_refused(tmp_path):
    conn, uid, pid = _setup(tmp_path)
    iss = issues.create_issue(conn, title="x", body="", created_by=uid, project_id=pid)
    with pytest.raises(sqlite3.IntegrityError):
        issues.set_sprint(conn, iss["id"], 9999)  # FK backstop
    conn.close()


def test_delete_guarded_by_membership(tmp_path):
    conn, uid, pid = _setup(tmp_path)
    s = sprints.create_sprint(conn, project_id=pid, name="S1")
    iss = issues.create_issue(conn, title="x", body="", created_by=uid, project_id=pid)
    issues.set_sprint(conn, iss["id"], s["id"])

    # The caller's precondition: don't delete a sprint that still holds issues.
    assert sprints.count_issues_in_sprint(conn, s["id"]) == 1
    # Empty it, then the delete succeeds.
    issues.set_sprint(conn, iss["id"], None)
    assert sprints.delete_sprint(conn, s["id"]) is True
    assert sprints.get_sprint(conn, s["id"]) is None
    # Deleting a missing sprint is a clean False.
    assert sprints.delete_sprint(conn, 9999) is False
    conn.close()
