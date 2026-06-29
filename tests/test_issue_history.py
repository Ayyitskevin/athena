"""Issue lifecycle time-travel — project_issue_state folds the log into state as-of.

issue_activity APPENDS a diff event per lifecycle change; issue_history reads those same
events back and folds them forward into the issue's state as of any event id. These tests
close the loop (recorder writes → projector reads): the current projection is the
last-of-each, an intermediate cutoff shows the state at that point, and a cutoff before
any change recovers the CREATION values (status/priority from the first change's recorded
`before`; assignee/labels/sprint/parent/archive from their empty born state). Honest
scope: only diff-logged fields are reconstructed — content (title/body) and project are
deliberately absent, never guessed.
"""
from fastapi.testclient import TestClient

from athena.aegis import issue_activity as ia
from athena.aegis import issue_history, issues
from athena.core import activity, db, labels
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name, role) VALUES ('a@e.com', 'Alice', 'admin')")
    conn.commit()
    return conn


def _id_of(conn, issue_id, verb, detail=None):
    """The id of the issue's event matching verb (and detail), oldest match."""
    rows = reversed(
        activity.list_activity(conn, target_kind="issue", target_id=issue_id, limit=1000)
    )
    for e in rows:
        if e["verb"] == verb and (detail is None or e["detail"] == detail):
            return e["id"]
    raise AssertionError(f"no {verb} event")


def test_projection_folds_status_priority_assignee_labels(tmp_path):
    conn = _conn(tmp_path / "fold.db")
    iid = issues.create_issue(conn, title="x", body="", created_by=1)["id"]  # open / medium
    bug = labels.get_or_create_label(conn, name="bug")["id"]

    ia.record_status_change(conn, actor_id=1, issue_id=iid, before="open", after="in_progress")
    ia.record_priority_change(conn, actor_id=1, issue_id=iid, before="medium", after="high")
    ia.record_assignee_change(conn, actor_id=1, issue_id=iid, before=None, after=1)
    ia.record_label_added(conn, actor_id=1, issue_id=iid, label_id=bug)
    checkpoint = _id_of(conn, iid, "labeled", "bug")  # state right after the bug label
    ia.record_status_change(conn, actor_id=1, issue_id=iid, before="in_progress", after="done")
    ia.record_label_removed(conn, actor_id=1, issue_id=iid, label_id=bug)

    # Now: the last value of each field.
    now = issue_history.project_issue_state(conn, iid)
    assert now["is_current"] is True
    assert now["state"]["status"] == "done" and now["state"]["priority"] == "high"
    assert now["state"]["assignee"] == "Alice"
    assert now["state"]["labels"] == [] and now["state"]["archived"] is False

    # At the labeling checkpoint: status was still in_progress, bug still attached.
    at = issue_history.project_issue_state(conn, iid, as_of_event_id=checkpoint)
    assert at["is_current"] is False
    assert at["state"]["status"] == "in_progress"
    assert at["state"]["priority"] == "high"
    assert at["state"]["labels"] == ["bug"]

    # Before anything happened: the CREATION values, recovered from the first change's
    # `before`; nothing assigned or labeled.
    start = issue_history.project_issue_state(conn, iid, as_of_event_id=0)
    assert start["state"]["status"] == "open" and start["state"]["priority"] == "medium"
    assert start["state"]["assignee"] is None and start["state"]["labels"] == []


def test_projection_sets_and_clears_parent_and_archive(tmp_path):
    conn = _conn(tmp_path / "pa.db")
    parent = issues.create_issue(conn, title="epic", body="", created_by=1)["id"]
    iid = issues.create_issue(conn, title="x", body="", created_by=1)["id"]

    ia.record_parent_change(conn, actor_id=1, issue_id=iid, before=None, after=parent)
    ia.record_archive_change(conn, actor_id=1, issue_id=iid, before=None, after="2026-01-01 00:00:00")
    both = issue_history.project_issue_state(conn, iid)["state"]
    assert both["parent"] == f"#{parent}" and both["archived"] is True

    # Clearing both folds back to the defaults.
    ia.record_parent_change(conn, actor_id=1, issue_id=iid, before=parent, after=None)
    ia.record_archive_change(
        conn, actor_id=1, issue_id=iid, before="2026-01-01 00:00:00", after=None
    )
    cleared = issue_history.project_issue_state(conn, iid)["state"]
    assert cleared["parent"] is None and cleared["archived"] is False


def test_unknown_issue_is_none(tmp_path):
    conn = _conn(tmp_path / "none.db")
    assert issue_history.project_issue_state(conn, 999) is None


# --- REST API ---------------------------------------------------------------


def test_state_endpoint_walks_history_and_gates(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
        client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
        client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)

        iid = client.post("/issues", json={"title": "x"}, headers=H_CREATOR).json()["id"]
        client.patch(f"/issues/{iid}", json={"status": "in_progress"}, headers=H_CREATOR)

        # Current state.
        now = client.get(f"/issues/{iid}/state", headers=H_CREATOR)
        assert now.status_code == 200
        assert now.json()["state"]["status"] == "in_progress" and now.json()["is_current"] is True

        # As of the created event (the floor): the original status, before the change.
        created_id = min(
            e["id"]
            for e in client.get(f"/activity?target_kind=issue&target_id={iid}", headers=H_CREATOR).json()
        )
        before = client.get(f"/issues/{iid}/state?as_of={created_id}", headers=H_CREATOR).json()
        assert before["state"]["status"] == "open" and before["is_current"] is False

        assert client.get("/issues/9999/state", headers=H_CREATOR).status_code == 404


def test_state_endpoint_is_visibility_gated(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
        client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
        client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H_CREATOR).json()["id"]
        iid = client.post("/issues", json={"title": "y", "project_id": pid}, headers=H_CREATOR).json()["id"]
        client.put(f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=H_CREATOR)

        # A hidden issue's state never leaks — same 404 as a missing one.
        assert client.get(f"/issues/{iid}/state", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/issues/{iid}/state", headers=H_CREATOR).status_code == 200
