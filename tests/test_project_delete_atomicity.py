"""A project's lifecycle event commits with its write, or not at all (ARCH-1, C2).

The trail is load-bearing: `athena-doctor` certifies the activity chain, so an
event that says a live project was deleted is a permanent, verified falsehood.
The review reproduced exactly that. `record_project_deleted` committed its event
inside `delete_project`'s transaction BEFORE the row was removed, and the
route's emptiness pre-checks ran outside that transaction. An issue created in
between made the DELETE fail at the foreign key: a 500, a project that still
exists, and a `deleted_project` event that never rolls back.

These pin the repaired shape: the emptiness checks live inside the command's
immediate transaction and refuse with a typed conflict (409 on both
transports), and every lifecycle recorder takes `commit=False` so a write that
fails after its event leaves neither the write nor the event behind.
"""

import pytest
from fastapi.testclient import TestClient

from athena.aegis import issue_commands, project_activity, project_commands, projects
from athena.aegis import sprints
from athena.core import activity_chain, db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _trail(conn):
    """The trail's size and certified head — both must be unchanged by a refusal."""
    count = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    return count, activity_chain.head(conn)


def _seeded(tmp_path, name):
    db_file = tmp_path / name
    with TestClient(create_app(db_file)) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        pid = c.post(
            "/projects", json={"name": "Vault", "key": "VLT"}, headers=H1
        ).json()["id"]
    return db_file, pid


def _concurrent_issue(db_file, project_id):
    """An issue created on ANOTHER connection, as a racing request would."""
    other = db.connect(db_file)
    try:
        issue_commands.create_issue(
            other,
            actor=users.get_user(other, 1),
            title="raced in",
            project_id=project_id,
        )
    finally:
        other.close()


def test_deleting_a_project_that_owns_an_issue_is_a_typed_refusal(tmp_path):
    db_file, pid = _seeded(tmp_path, "issue.db")
    _concurrent_issue(db_file, pid)
    conn = db.connect(db_file)
    try:
        before = _trail(conn)
        with pytest.raises(Exception) as refused:
            project_commands.delete_project(conn, actor_id=1, project_id=pid)
        # Nothing reached the trail: no phantom 'deleted_project', same chain head.
        assert _trail(conn) == before
        assert projects.get_project(conn, pid) is not None
        assert isinstance(refused.value, project_commands.ProjectLifecycleCommandError)
        assert refused.value.kind == "conflict"
        assert "issues" in refused.value.detail
    finally:
        conn.close()


def test_deleting_a_project_that_owns_a_sprint_is_a_typed_refusal(tmp_path):
    db_file, pid = _seeded(tmp_path, "sprint.db")
    conn = db.connect(db_file)
    try:
        sprints.create_sprint(conn, project_id=pid, name="Launch", commit=True)
        before = _trail(conn)
        with pytest.raises(Exception) as refused:
            project_commands.delete_project(conn, actor_id=1, project_id=pid)
        assert _trail(conn) == before
        assert projects.get_project(conn, pid) is not None
        assert isinstance(refused.value, project_commands.ProjectLifecycleCommandError)
        assert refused.value.kind == "conflict"
        assert "sprints" in refused.value.detail
    finally:
        conn.close()


def _fail_after(monkeypatch, recorder_name):
    """Make the named lifecycle recorder write its event and THEN fail — the shape
    of any later step in the command going wrong after the event."""
    real = getattr(project_activity, recorder_name)

    def record_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("the write failed after its event")

    monkeypatch.setattr(project_activity, recorder_name, record_then_fail)


def test_a_create_that_fails_after_its_event_leaves_nothing(tmp_path, monkeypatch):
    db_file, _ = _seeded(tmp_path, "create.db")
    conn = db.connect(db_file)
    try:
        before = _trail(conn)
        _fail_after(monkeypatch, "record_project_created")
        with pytest.raises(RuntimeError):
            project_commands.create_project(conn, actor_id=1, name="Ghost", key="GST")
        assert _trail(conn) == before
        assert projects.get_project_by_key(conn, "GST") is None
    finally:
        conn.close()


def test_an_edit_that_fails_after_its_event_leaves_nothing(tmp_path, monkeypatch):
    db_file, pid = _seeded(tmp_path, "edit.db")
    conn = db.connect(db_file)
    try:
        before = _trail(conn)
        _fail_after(monkeypatch, "record_project_edited")
        with pytest.raises(RuntimeError):
            project_commands.update_project(
                conn, actor_id=1, project_id=pid, name="Renamed"
            )
        assert _trail(conn) == before
        assert projects.get_project(conn, pid)["name"] == "Vault"
    finally:
        conn.close()


def test_a_delete_that_fails_after_its_event_leaves_nothing(tmp_path, monkeypatch):
    db_file, pid = _seeded(tmp_path, "delete.db")
    conn = db.connect(db_file)
    try:
        before = _trail(conn)
        _fail_after(monkeypatch, "record_project_deleted")
        with pytest.raises(RuntimeError):
            project_commands.delete_project(conn, actor_id=1, project_id=pid)
        assert _trail(conn) == before
        assert projects.get_project(conn, pid) is not None
    finally:
        conn.close()


def _race_the_delete(monkeypatch, db_file, pid, seen):
    """Create an issue after the transport has done its own work and before the
    command runs — the window the review's 5-in-24 race landed in."""
    real = project_commands.delete_project

    def raced(conn, *, actor_id, project_id):
        _concurrent_issue(db_file, pid)
        probe = db.connect(db_file)
        try:
            seen["before"] = _trail(probe)
        finally:
            probe.close()
        return real(conn, actor_id=actor_id, project_id=project_id)

    monkeypatch.setattr(project_commands, "delete_project", raced)


def _assert_refused_cleanly(db_file, pid, seen):
    conn = db.connect(db_file)
    try:
        assert _trail(conn) == seen["before"]
        assert projects.get_project(conn, pid) is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM activity WHERE verb = 'deleted_project'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_rest_delete_racing_an_issue_create_is_a_409_not_a_500(tmp_path, monkeypatch):
    db_file, pid = _seeded(tmp_path, "rest_race.db")
    seen: dict = {}
    _race_the_delete(monkeypatch, db_file, pid, seen)
    with TestClient(create_app(db_file), raise_server_exceptions=False) as c:
        r = c.delete(f"/projects/{pid}", headers=H1)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "reassign or delete its issues first"
    _assert_refused_cleanly(db_file, pid, seen)


def test_web_delete_racing_an_issue_create_is_a_409_not_a_500(tmp_path, monkeypatch):
    db_file, pid = _seeded(tmp_path, "web_race.db")
    seen: dict = {}
    _race_the_delete(monkeypatch, db_file, pid, seen)
    with TestClient(create_app(db_file), raise_server_exceptions=False) as c:
        c.post("/login", data={"email": "a@e.com", "password": "pw"})
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        r = c.post(f"/aegis/projects/{pid}/delete", follow_redirects=False)
    assert r.status_code == 409, r.text
    assert "issues first" in r.text
    _assert_refused_cleanly(db_file, pid, seen)
