"""Cross-entity search respects visibility (slice 4 — the cross-entity surfaces).

Global search ranks issues AND pages together, so it's the widest leak: a private
project's issues and a private space's pages must not surface to someone who can't see
them, on the REST cross-kind search, the REST issue search, and the web /find page —
while admins, the creator, and members get the full ranked set. The shared term
"Zephyr" lets one query reach all four documents so we can see exactly which survive.
"""

from fastapi.testclient import TestClient

from athena.core import access, db
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


def _scenario(client, db_file):
    """A hidden + a public issue, a hidden + a public page, all matching "Zephyr".
    Returns (db_conn, priv_project_id, priv_space_id)."""
    pp = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    op = client.post(
        "/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR
    ).json()["id"]
    client.post(
        "/issues", json={"title": "Zephyr alpha", "project_id": pp}, headers=H_CREATOR
    )
    client.post(
        "/issues", json={"title": "Zephyr beta", "project_id": op}, headers=H_CREATOR
    )
    ps = client.post(
        "/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR
    ).json()["id"]
    os_ = client.post(
        "/spaces", json={"key": "OSP", "name": "OpenSpace"}, headers=H_CREATOR
    ).json()["id"]
    client.post(
        f"/spaces/{ps}/pages", json={"title": "Zephyr gamma"}, headers=H_CREATOR
    )
    client.post(
        f"/spaces/{os_}/pages", json={"title": "Zephyr delta"}, headers=H_CREATOR
    )

    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
    conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
    conn.commit()
    return conn, pp, ps


def _login(client, email):
    r = client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_rest_cross_kind_search_gates_both_kinds(tmp_path):
    db_file = tmp_path / "search.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, pp, ps = _scenario(client, db_file)

        def titles(headers):
            return {
                h["title"]
                for h in client.get("/search?q=Zephyr", headers=headers).json()
            }

        # Outsider sees only the public issue + public page.
        assert titles(H_OUTSIDER) == {"Zephyr beta", "Zephyr delta"}
        # Creator and admin see all four.
        assert titles(H_CREATOR) == {
            "Zephyr alpha",
            "Zephyr beta",
            "Zephyr gamma",
            "Zephyr delta",
        }
        assert titles(H_ADMIN) == {
            "Zephyr alpha",
            "Zephyr beta",
            "Zephyr gamma",
            "Zephyr delta",
        }

        # Granting membership on both private containers reveals the hidden hits.
        access.add_project_member(conn, pp, 3, added_by=2)
        access.add_space_member(conn, ps, 3, added_by=2)
        assert titles(H_OUTSIDER) == {
            "Zephyr alpha",
            "Zephyr beta",
            "Zephyr gamma",
            "Zephyr delta",
        }


def test_rest_issue_search_gates_private_issue(tmp_path):
    db_file = tmp_path / "isearch.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        def titles(headers=None):
            return {
                h["title"]
                for h in client.get(
                    "/issues/search?q=Zephyr", headers=headers or {}
                ).json()
            }

        # The issue search (issues only) hides the private issue from outsider + anon.
        for h in (H_OUTSIDER, None):
            assert titles(h) == {"Zephyr beta"}
        assert titles(H_CREATOR) == {"Zephyr alpha", "Zephyr beta"}


def test_web_find_hides_private_hits(tmp_path):
    db_file = tmp_path / "find.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        anon = client.get("/find?q=Zephyr").text
        assert "Zephyr alpha" not in anon and "Zephyr gamma" not in anon
        assert "Zephyr beta" in anon and "Zephyr delta" in anon

        # Signed in as the creator, the hidden hits come back.
        _login(client, "c@e.com")
        mine = client.get("/find?q=Zephyr").text
        assert "Zephyr alpha" in mine and "Zephyr gamma" in mine
