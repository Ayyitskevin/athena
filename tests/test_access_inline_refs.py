"""Inline [[ref]] cross-link rendering respects visibility (slice 4 follow-up).

render_body turns a [[issue:N]] / [[page:N]] / [[KEY-N]] token into a link carrying
the target's title. That link is a read surface like any other: a token pointing at an
issue in a private project (or a page in a private space) must leak neither the hidden
title nor its existence. So a hidden-but-real target renders BROKEN — the same dangling
token a deleted target gets — indistinguishable from a genuine miss. Admins, the
creator, and members still get the live link. The _UNGATED default (internal/test
callers) resolves everything.
"""

from fastapi.testclient import TestClient

from athena.core import access, db, users
from athena.main import create_app
from athena.web.render import render_body

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


def test_render_body_gates_hidden_issue_ref(tmp_path):
    db_file = tmp_path / "ir.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hidden = client.post(
            "/issues",
            json={"title": "TopSecretTitle", "project_id": pp},
            headers=H_CREATOR,
        ).json()
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        body = f"see [[issue:{hidden['id']}]]"
        href = f'href="/aegis/issues/{hidden["id"]}"'
        # Outsider and anonymous: the hidden title never appears, and the token renders
        # broken rather than linking — broken-because-hidden looks like a dead ref.
        for actor in (None, users.get_user(conn, 3)):
            html = str(render_body(conn, body, actor=actor))
            assert "TopSecretTitle" not in html
            assert "xref broken" in html
            assert href not in html
        # Admin and creator get the live link with the title.
        for uid in (1, 2):
            html = str(render_body(conn, body, actor=users.get_user(conn, uid)))
            assert "TopSecretTitle" in html
            assert f'{href} class="xref"' in html
        # Membership reveals it to the outsider.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert "TopSecretTitle" in str(
            render_body(conn, body, actor=users.get_user(conn, 3))
        )
        # The _UNGATED default (internal caller) resolves everything, unchanged.
        assert "TopSecretTitle" in str(render_body(conn, body))


def test_render_body_gates_hidden_key_ref(tmp_path):
    db_file = tmp_path / "kr.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        client.post(
            "/issues",
            json={"title": "TopSecretTitle", "project_id": pp},
            headers=H_CREATOR,
        )
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        body = "via [[SEC-1]]"  # the project key + the issue's per-project number
        out = str(render_body(conn, body, actor=users.get_user(conn, 3)))
        assert "TopSecretTitle" not in out and "xref broken" in out
        creator = str(render_body(conn, body, actor=users.get_user(conn, 2)))
        assert "TopSecretTitle" in creator and 'class="xref"' in creator


def test_render_body_gates_hidden_page_ref(tmp_path):
    db_file = tmp_path / "pr.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        ps = client.post(
            "/spaces", json={"key": "SSP", "name": "S"}, headers=H_CREATOR
        ).json()["id"]
        hp = client.post(
            f"/spaces/{ps}/pages", json={"title": "HushHushPage"}, headers=H_CREATOR
        ).json()
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
        conn.commit()

        body = f"ref [[page:{hp['id']}]]"
        href = f'href="/mentor/pages/{hp["id"]}"'
        for actor in (None, users.get_user(conn, 3)):
            html = str(render_body(conn, body, actor=actor))
            assert (
                "HushHushPage" not in html
                and "xref broken" in html
                and href not in html
            )
        for uid in (1, 2):
            html = str(render_body(conn, body, actor=users.get_user(conn, uid)))
            assert "HushHushPage" in html and f'{href} class="xref"' in html
        access.add_space_member(conn, ps, 3, added_by=2)
        assert "HushHushPage" in str(
            render_body(conn, body, actor=users.get_user(conn, 3))
        )


def test_issue_detail_page_gates_inline_ref_end_to_end(tmp_path):
    """The web issue-detail route threads the viewer into render_body: a public host
    issue whose body links a hidden one shows the hidden title to nobody who can't see
    it, and the live link to those who can."""
    db_file = tmp_path / "e2e.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hidden = client.post(
            "/issues",
            json={"title": "TopSecretTitle", "project_id": pp},
            headers=H_CREATOR,
        ).json()
        # A backlog issue (no project → always public) whose body references the hidden one.
        host = client.post(
            "/issues",
            json={"title": "Host", "body": f"see [[issue:{hidden['id']}]]"},
            headers=H_CREATOR,
        ).json()
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        # Anonymous viewer of the public host issue: hidden title absent, ref broken.
        anon = client.get(f"/aegis/issues/{host['id']}").text
        assert "TopSecretTitle" not in anon and "xref broken" in anon
        # The signed-in creator sees the live link.
        _login(client, "c@e.com")
        signed = client.get(f"/aegis/issues/{host['id']}").text
        assert "TopSecretTitle" in signed
