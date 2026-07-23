"""Existence oracles + dropdown leaks found by the access-control audit (group 3).

Three remaining gaps: (1) GET /issues/{id}/backlinks used a bare existence check, so a
hidden issue returned 200 (with a viewer-gated list) vs 404 for a missing one — a
200-vs-404 existence oracle; it now funnels through _issue_for_read. (2) The REST
privacy-manage gates (_project_for_privacy / _space_for_privacy) returned 403 for a
private container the actor can't see, leaking its existence; they now 404 first, like
their web twins. (3) The issue-list / board status filter and the sprint filter
enumerated names from private projects; they now restrict to what the viewer may see.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post(
        "/users",
        json={"email": "admin@e.com", "name": "Admin", "password": "pw"},
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "c@e.com",
            "name": "Creator",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "o@e.com",
            "name": "Outsider",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )


def _login(client, email):
    r = client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_backlinks_existence_oracle_closed(tmp_path):
    db_file = tmp_path / "bl.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hidden = client.post(
            "/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.commit()

        # The hidden issue's backlinks 404 for the outsider/anon — identical to a wholly
        # nonexistent id, so the 200-vs-404 oracle is gone; the creator gets 200.
        assert (
            client.get(f"/issues/{hidden}/backlinks", headers=H_OUTSIDER).status_code
            == 404
        )
        assert client.get(f"/issues/{hidden}/backlinks").status_code == 404
        assert (
            client.get("/issues/999999/backlinks", headers=H_OUTSIDER).status_code
            == 404
        )
        assert (
            client.get(f"/issues/{hidden}/backlinks", headers=H_CREATOR).status_code
            == 200
        )


def test_privacy_manage_404s_for_outsider_on_private(tmp_path):
    db_file = tmp_path / "pm.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        sp = client.post(
            "/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.execute("UPDATE spaces SET visibility='private' WHERE id=?", (sp,))
        conn.commit()

        # An outsider who can't see the private project/space gets 404 (not the 403 that
        # would reveal it exists) on every manage path — matching the web twins.
        assert (
            client.put(
                f"/projects/{pp}/visibility",
                json={"visibility": "public"},
                headers=H_OUTSIDER,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/projects/{pp}/members", json={"user_id": 3}, headers=H_OUTSIDER
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/projects/{pp}/members/2", headers=H_OUTSIDER).status_code
            == 404
        )
        assert (
            client.put(
                f"/spaces/{sp}/visibility",
                json={"visibility": "public"},
                headers=H_OUTSIDER,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/spaces/{sp}/members", json={"user_id": 3}, headers=H_OUTSIDER
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/spaces/{sp}/members/2", headers=H_OUTSIDER).status_code
            == 404
        )
        # The creator still manages (200), and a member who can SEE it but isn't the
        # creator/admin gets the honest 403 (existence is no longer secret to them).
        assert (
            client.put(
                f"/projects/{pp}/visibility",
                json={"visibility": "private"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        from athena.core import access

        access.add_project_member(conn, pp, 3, added_by=2)
        assert (
            client.put(
                f"/projects/{pp}/visibility",
                json={"visibility": "private"},
                headers=H_OUTSIDER,
            ).status_code
            == 403
        )


def test_status_filter_dropdown_hides_private_custom_status(tmp_path):
    db_file = tmp_path / "sd.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        # A distinctively-named custom status on the private project, used by an issue.
        client.post(
            f"/projects/{pp}/statuses",
            json={"name": "secretphase", "category": "doing"},
            headers=H_CREATOR,
        )
        client.post(
            "/issues",
            json={"title": "h", "project_id": pp, "status": "secretphase"},
            headers=H_CREATOR,
        )
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.commit()

        # The custom status name is absent from the issue-list and board filter dropdowns
        # for an anonymous viewer; present for the signed-in creator.
        assert "secretphase" not in client.get("/aegis/issues").text
        assert "secretphase" not in client.get("/aegis/boards").text
        _login(client, "c@e.com")
        assert "secretphase" in client.get("/aegis/issues").text


def test_sprint_filter_dropdown_hides_private_sprint(tmp_path):
    db_file = tmp_path / "spd.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        client.post(
            f"/projects/{pp}/sprints",
            json={"name": "secretsprintname"},
            headers=H_CREATOR,
        )
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.commit()

        # The private project's sprint name is absent from the issue-list sprint dropdown
        # for an anonymous viewer; present for the signed-in creator.
        assert "secretsprintname" not in client.get("/aegis/issues").text
        _login(client, "c@e.com")
        assert "secretsprintname" in client.get("/aegis/issues").text
