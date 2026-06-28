"""Writes require visibility — "can't write what you can't see" (slice 5, REST).

The privacy switch is live, so now every WRITE to a private project/space must be gated
by the same visibility the reads are: an actor who can't see a container can't mutate
anything in it, and the block is a 404 (indistinguishable from missing) so a hidden
resource's existence never leaks through a write attempt.

The gates concentrate at choke points: _issue_for_write (issue mutations),
_issue_for_read (issue comments/attachments — visibility without modify rights),
_project_for_sprint_write (sprint mutations), and _page_for_read (page mutations). These
tests exercise each, plus the subtle cases: an issue owner who loses access when the
project goes private, creating/moving into a hidden project, and linking to a hidden
issue (an existence oracle, now closed).
"""
from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


def _private_project_with_issue(client, db_file):
    pid = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
    iid = client.post("/issues", json={"title": "Hidden", "project_id": pid}, headers=H_CREATOR).json()["id"]
    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pid,))
    conn.commit()
    return pid, iid


# --- Issue writes ----------------------------------------------------------


def test_issue_writes_require_visibility(tmp_path):
    db_file = tmp_path / "iw.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid, iid = _private_project_with_issue(client, db_file)
        lid = client.post("/labels", json={"name": "x", "color": "#ff0000"}, headers=H_CREATOR).json()["id"]

        # modify-gated writes: an outsider who can't see it gets 404 (not 403 — no leak).
        assert client.patch(f"/issues/{iid}", json={"title": "x"}, headers=H_OUTSIDER).status_code == 404
        assert client.post(f"/issues/{iid}/archive", headers=H_OUTSIDER).status_code == 404
        assert client.post(f"/issues/{iid}/labels", json={"label_id": lid}, headers=H_OUTSIDER).status_code == 404
        # visibility-only writes (comments) are also 404 for the outsider.
        assert client.post(f"/issues/{iid}/comments", json={"body": "hi"}, headers=H_OUTSIDER).status_code == 404
        # The project creator (who can see it AND owns the issue) writes fine, including
        # the modify-gated label attach.
        assert client.patch(f"/issues/{iid}", json={"title": "ok"}, headers=H_CREATOR).status_code == 200
        assert client.post(f"/issues/{iid}/labels", json={"label_id": lid}, headers=H_CREATOR).status_code == 201
        assert client.post(f"/issues/{iid}/comments", json={"body": "hi"}, headers=H_CREATOR).status_code == 201
        # Admin's god view grants VISIBILITY, so a visibility-only write (comment)
        # succeeds — but admin is still not the issue's creator/assignee, so a
        # modify-gated write (label attach) is a 403, not bypassed by the god view.
        assert client.post(f"/issues/{iid}/comments", json={"body": "adm"}, headers=H_ADMIN).status_code == 201
        assert client.post(f"/issues/{iid}/labels", json={"label_id": lid}, headers=H_ADMIN).status_code == 403

        # Membership grants VISIBILITY: the 404 turns into a 403 on a modify-gated write
        # (the member can now see it but isn't its creator/assignee), and a comment —
        # which needs only visibility — now succeeds. This proves the 404 was the
        # visibility gate, distinct from the modify gate.
        conn = db.connect(db_file)
        access.add_project_member(conn, pid, 3, added_by=2)
        assert client.patch(f"/issues/{iid}", json={"title": "y"}, headers=H_OUTSIDER).status_code == 403
        assert client.post(f"/issues/{iid}/comments", json={"body": "yo"}, headers=H_OUTSIDER).status_code == 201


def test_issue_owner_loses_write_when_project_goes_private(tmp_path):
    """The subtle case: you created the issue, but the project was flipped private
    without adding you. You can no longer SEE it, so you can no longer write it — even
    though can_modify (creator/assignee) would otherwise say yes."""
    db_file = tmp_path / "il.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        # Admin owns a public project; the outsider creates (and is assigned) an issue.
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H_ADMIN).json()["id"]
        iid = client.post("/issues", json={"title": "mine", "project_id": pid}, headers=H_OUTSIDER).json()["id"]
        assert client.patch(f"/issues/{iid}", json={"title": "still mine"}, headers=H_OUTSIDER).status_code == 200
        # Admin flips the project private without adding the outsider.
        client.put(f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=H_ADMIN)
        # The outsider is the issue's creator+assignee, but can no longer see it → 404.
        assert client.patch(f"/issues/{iid}", json={"title": "nope"}, headers=H_OUTSIDER).status_code == 404


def test_create_and_move_into_private_project_blocked(tmp_path):
    db_file = tmp_path / "cm.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid, _ = _private_project_with_issue(client, db_file)
        # Creating an issue in a project you can't see looks like the project doesn't
        # exist (422), no leak.
        assert client.post("/issues", json={"title": "x", "project_id": pid}, headers=H_OUTSIDER).status_code == 422
        # Nor can you move a backlog issue you own INTO it.
        biid = client.post("/issues", json={"title": "b"}, headers=H_OUTSIDER).json()["id"]
        assert client.put(f"/issues/{biid}/project", json={"project_id": pid}, headers=H_OUTSIDER).status_code == 422
        # The creator (who can see it) can create in it.
        assert client.post("/issues", json={"title": "y", "project_id": pid}, headers=H_CREATOR).status_code == 201


def test_link_to_hidden_issue_blocked(tmp_path):
    db_file = tmp_path / "lk.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid, hidden = _private_project_with_issue(client, db_file)
        # The outsider owns a backlog issue and tries to declare a dependency on the
        # hidden one by id — same 422 as a nonexistent target (no existence oracle).
        biid = client.post("/issues", json={"title": "b"}, headers=H_OUTSIDER).json()["id"]
        r = client.post(
            f"/issues/{biid}/links",
            json={"relation": "blocks", "target_ref": str(hidden)},
            headers=H_OUTSIDER,
        )
        assert r.status_code == 422
        # The creator can see the hidden issue, so linking their own backlog issue to it
        # works.
        cbiid = client.post("/issues", json={"title": "cb"}, headers=H_CREATOR).json()["id"]
        ok = client.post(
            f"/issues/{cbiid}/links",
            json={"relation": "blocks", "target_ref": str(hidden)},
            headers=H_CREATOR,
        )
        assert ok.status_code == 201


# --- Sprint writes ---------------------------------------------------------


def test_sprint_writes_require_visibility_without_leak(tmp_path):
    db_file = tmp_path / "sp.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid, _ = _private_project_with_issue(client, db_file)
        # An outsider can't manage sprints AND gets 404 (not 403) — the private project's
        # existence doesn't leak through a sprint-write attempt.
        assert client.post(f"/projects/{pid}/sprints", json={"name": "S"}, headers=H_OUTSIDER).status_code == 404
        # The creator can.
        assert client.post(f"/projects/{pid}/sprints", json={"name": "S"}, headers=H_CREATOR).status_code == 201
        # A member who CAN see the project but isn't its creator gets the honest 403 —
        # sprint management stays creator-only, and they already know it exists.
        conn = db.connect(db_file)
        access.add_project_member(conn, pid, 3, added_by=2)
        assert client.post(f"/projects/{pid}/sprints", json={"name": "S2"}, headers=H_OUTSIDER).status_code == 403


# --- Page / space writes ---------------------------------------------------


def test_page_writes_require_visibility(tmp_path):
    db_file = tmp_path / "pw.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
        pid = client.post(f"/spaces/{sid}/pages", json={"title": "P", "body": "v1"}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (sid,))
        conn.commit()

        # Every page write 404s for an outsider on a hidden page/space.
        assert client.patch(f"/pages/{pid}", json={"body": "x"}, headers=H_OUTSIDER).status_code == 404
        assert client.post(f"/pages/{pid}/comments", json={"body": "x"}, headers=H_OUTSIDER).status_code == 404
        assert client.post(f"/spaces/{sid}/pages", json={"title": "new"}, headers=H_OUTSIDER).status_code == 404
        assert client.patch(f"/spaces/{sid}", json={"name": "x"}, headers=H_OUTSIDER).status_code == 404
        # The creator edits fine.
        assert client.patch(f"/pages/{pid}", json={"body": "ok"}, headers=H_CREATOR).status_code == 200
        # Mentor is a shared wiki: a MEMBER (who can see it) may edit — membership opens
        # the write to 200, not just past the 404 (unlike issues, no per-row modify lock).
        access.add_space_member(conn, sid, 3, added_by=2)
        assert client.patch(f"/pages/{pid}", json={"body": "member"}, headers=H_OUTSIDER).status_code == 200
