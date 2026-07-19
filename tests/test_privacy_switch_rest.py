"""The privacy switch + member management, at the REST layer (slice 5, the switch-on).

This is where the access feature stops being inert: PUT /{project,space}/{id}/visibility
flips the row private, and the member endpoints grant/revoke access. The gate is
creator-OR-admin for managing access (wider than edit/delete, which stay creator-only);
reading the roster is gated by plain visibility. Going private auto-records the creator
as a member, and every transition lands an audit event the activity gate hides from
outsiders.

The read-gating slices already shipped, so these tests also assert the *integration*:
once flipped private, the project/space drops out of the list and 404s for an outsider —
proving the switch actually engages the resolver wired in earlier.
"""
from fastapi.testclient import TestClient

from athena.core import activity, db, users
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


# --- Projects --------------------------------------------------------------


def test_project_visibility_toggle_authz(tmp_path):
    db_file = tmp_path / "pv.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H_CREATOR).json()["id"]
        url = f"/projects/{pid}/visibility"

        # Anonymous (no actor) can't write at all.
        assert client.put(url, json={"visibility": "private"}).status_code == 401
        # An outsider who isn't the creator or an admin is forbidden.
        assert client.put(url, json={"visibility": "private"}, headers=H_OUTSIDER).status_code == 403
        # A bad value is rejected before any write.
        assert client.put(url, json={"visibility": "secret"}, headers=H_CREATOR).status_code == 422

        # The creator may flip it; the response echoes the new visibility.
        r = client.put(url, json={"visibility": "private"}, headers=H_CREATOR)
        assert r.status_code == 200 and r.json()["visibility"] == "private"
        # An admin may flip anyone's project back to public.
        r = client.put(url, json={"visibility": "public"}, headers=H_ADMIN)
        assert r.status_code == 200 and r.json()["visibility"] == "public"


def test_going_private_hides_project_and_auto_adds_creator(tmp_path):
    db_file = tmp_path / "ph.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        # Public: everyone sees it in the list and on its detail.
        assert any(p["id"] == pid for p in client.get("/projects", headers=H_OUTSIDER).json())

        client.put(f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=H_CREATOR)

        # The read gate (shipped earlier) now engages: the outsider no longer lists or
        # shows it, while the creator and admin still do.
        assert not any(p["id"] == pid for p in client.get("/projects", headers=H_OUTSIDER).json())
        assert client.get(f"/projects/{pid}", headers=H_OUTSIDER).status_code == 404
        assert any(p["id"] == pid for p in client.get("/projects", headers=H_CREATOR).json())
        assert client.get(f"/projects/{pid}", headers=H_ADMIN).status_code == 200
        # The status vocabulary is project metadata — gated the same way (was a leak
        # before this slice, since GET /projects/{id}/statuses ran ungated).
        assert client.get(f"/projects/{pid}/statuses", headers=H_OUTSIDER).status_code == 404
        assert client.get(f"/projects/{pid}/statuses", headers=H_CREATOR).status_code == 200

        # Going private recorded the creator as an explicit member (for the roster UI).
        members = client.get(f"/projects/{pid}/members", headers=H_CREATOR).json()
        assert [m["user_id"] for m in members] == [2]


def test_project_member_lifecycle_and_roster_gate(tmp_path):
    db_file = tmp_path / "pm.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        client.put(f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=H_CREATOR)
        members_url = f"/projects/{pid}/members"

        # Outsider can't even read the roster of a project they can't see.
        assert client.get(members_url, headers=H_OUTSIDER).status_code == 404
        # An unknown user id is a clean 422, not a 500 from the FK.
        assert client.post(members_url, json={"user_id": 999}, headers=H_CREATOR).status_code == 422
        # An outsider who can't even SEE this private project gets 404 (not 403) on a
        # manage attempt — its existence stays hidden (audit fix: no 403 oracle).
        assert client.post(members_url, json={"user_id": 3}, headers=H_OUTSIDER).status_code == 404

        # The creator grants the outsider access; the roster now lists them.
        r = client.post(members_url, json={"user_id": 3}, headers=H_CREATOR)
        assert r.status_code == 201 and 3 in [m["user_id"] for m in r.json()]
        # And the grant actually opens the door: the outsider now sees the project.
        assert client.get(f"/projects/{pid}", headers=H_OUTSIDER).status_code == 200
        assert client.get(members_url, headers=H_OUTSIDER).status_code == 200

        # Re-adding is idempotent (still 201, roster unchanged — no duplicate).
        r = client.post(members_url, json={"user_id": 3}, headers=H_CREATOR)
        assert [m["user_id"] for m in r.json()].count(3) == 1

        # Removing a non-member is a 404; removing the member revokes access.
        assert client.delete(f"{members_url}/999", headers=H_CREATOR).status_code == 404
        assert client.delete(f"{members_url}/3", headers=H_ADMIN).status_code == 200
        assert client.get(f"/projects/{pid}", headers=H_OUTSIDER).status_code == 404


def test_project_visibility_event_is_recorded_and_gated(tmp_path):
    db_file = tmp_path / "pe.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        client.put(f"/projects/{pid}/visibility", json={"visibility": "private"}, headers=H_CREATOR)

        conn = db.connect(db_file)
        creator = users.get_user(conn, 2)
        outsider = users.get_user(conn, 3)

        def made_private(actor):
            return [
                e for e in activity.list_activity(conn, target_kind="project", target_id=pid, actor=actor)
                if e["verb"] == "project_made_private"
            ]

        # The creator sees the audit event; the outsider (can't see the project) does not.
        assert len(made_private(creator)) == 1
        assert made_private(outsider) == []


def test_visibility_set_to_same_is_noop(tmp_path):
    db_file = tmp_path / "pn.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = client.post("/projects", json={"name": "P", "key": "P"}, headers=H_CREATOR).json()["id"]
        # Setting public→public writes nothing and records no VISIBILITY event.
        # (Creating the project itself now records a 'created_project' event —
        # the lifecycle-audit slice — so filter to the visibility verbs.)
        client.put(f"/projects/{pid}/visibility", json={"visibility": "public"}, headers=H_CREATOR)
        conn = db.connect(db_file)
        rows = activity.list_activity(conn, target_kind="project", target_id=pid)
        vis_verbs = {"project_made_private", "project_made_public"}
        assert [r for r in rows if r["verb"] in vis_verbs] == []


# --- Spaces (the Mentor twin — same mechanism) -----------------------------


def test_space_visibility_toggle_and_member_lifecycle(tmp_path):
    db_file = tmp_path / "sv.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
        vis_url = f"/spaces/{sid}/visibility"

        assert client.put(vis_url, json={"visibility": "private"}).status_code == 401
        assert client.put(vis_url, json={"visibility": "private"}, headers=H_OUTSIDER).status_code == 403
        assert client.put(vis_url, json={"visibility": "nope"}, headers=H_CREATOR).status_code == 422

        r = client.put(vis_url, json={"visibility": "private"}, headers=H_CREATOR)
        assert r.status_code == 200 and r.json()["visibility"] == "private"

        # The read gate engages: the outsider can't list or show it; the creator can.
        assert not any(s["id"] == sid for s in client.get("/spaces", headers=H_OUTSIDER).json())
        assert client.get(f"/spaces/{sid}", headers=H_OUTSIDER).status_code == 404
        # Creator auto-recorded as a member.
        assert [m["user_id"] for m in client.get(f"/spaces/{sid}/members", headers=H_CREATOR).json()] == [2]

        members_url = f"/spaces/{sid}/members"
        # Roster gated; grant opens access; revoke closes it.
        assert client.get(members_url, headers=H_OUTSIDER).status_code == 404
        assert client.post(members_url, json={"user_id": 3}, headers=H_CREATOR).status_code == 201
        assert client.get(f"/spaces/{sid}", headers=H_OUTSIDER).status_code == 200
        assert client.delete(f"{members_url}/3", headers=H_CREATOR).status_code == 200
        assert client.get(f"/spaces/{sid}", headers=H_OUTSIDER).status_code == 404


def test_space_visibility_event_recorded_and_gated(tmp_path):
    db_file = tmp_path / "se.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
        client.put(f"/spaces/{sid}/visibility", json={"visibility": "private"}, headers=H_CREATOR)
        conn = db.connect(db_file)
        creator, outsider = users.get_user(conn, 2), users.get_user(conn, 3)

        def made_private(actor):
            return [
                e for e in activity.list_activity(conn, target_kind="space", target_id=sid, actor=actor)
                if e["verb"] == "space_made_private"
            ]

        assert len(made_private(creator)) == 1
        assert made_private(outsider) == []
