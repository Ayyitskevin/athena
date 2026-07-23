"""Cross-link "Referenced by" respects visibility (slice 4 — cross-entity surfaces).

A page/issue's backlink list gathers the issues AND pages that reference it. A source
in a private project (issue) or private space (page) must not reveal itself in a
public target's "Referenced by", while admins, the creator, and members see it. (The
inline [[ref]] rendering inside a body is a separate, tracked follow-up.)
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


def test_page_backlinks_hide_a_private_issue_source(tmp_path):
    db_file = tmp_path / "pb.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        # A public target page, referenced by a hidden issue and a public issue.
        sid = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H_CREATOR
        ).json()["id"]
        pg = client.post(
            f"/spaces/{sid}/pages", json={"title": "Target"}, headers=H_CREATOR
        ).json()["id"]
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hi = client.post(
            "/issues",
            json={
                "title": "Hidden ref",
                "project_id": pp,
                "body": f"see [[page:{pg}]]",
            },
            headers=H_CREATOR,
        ).json()["id"]
        pi = client.post(
            "/issues",
            json={"title": "Public ref", "body": f"see [[page:{pg}]]"},
            headers=H_CREATOR,
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        def sources(headers=None):
            return {
                (b["kind"], b["id"])
                for b in client.get(
                    f"/pages/{pg}/backlinks", headers=headers or {}
                ).json()
            }

        # Outsider + anonymous: only the public issue references; never the hidden one.
        for h in (H_OUTSIDER, None):
            s = sources(h)
            assert ("issue", pi) in s and ("issue", hi) not in s
        # Creator + admin see both.
        for h in (H_CREATOR, H_ADMIN):
            assert ("issue", hi) in sources(h)

        # The web "Referenced by" matches: anonymous doesn't see the hidden source title.
        text = client.get(f"/mentor/pages/{pg}").text
        assert "Public ref" in text and "Hidden ref" not in text

        # Membership reveals it.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert ("issue", hi) in sources(H_OUTSIDER)


def test_issue_backlinks_hide_a_private_page_source(tmp_path):
    db_file = tmp_path / "ib.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        # A public target issue, referenced by a hidden page and a public page.
        target = client.post(
            "/issues", json={"title": "Target issue"}, headers=H_CREATOR
        ).json()["id"]
        ps = client.post(
            "/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR
        ).json()["id"]
        os_ = client.post(
            "/spaces", json={"key": "OSP", "name": "OpenSpace"}, headers=H_CREATOR
        ).json()["id"]
        hp = client.post(
            f"/spaces/{ps}/pages",
            json={"title": "Hidden doc", "body": f"see [[issue:{target}]]"},
            headers=H_CREATOR,
        ).json()["id"]
        pp_ = client.post(
            f"/spaces/{os_}/pages",
            json={"title": "Public doc", "body": f"see [[issue:{target}]]"},
            headers=H_CREATOR,
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
        conn.commit()

        def sources(headers=None):
            return {
                (b["kind"], b["id"])
                for b in client.get(
                    f"/issues/{target}/backlinks", headers=headers or {}
                ).json()
            }

        for h in (H_OUTSIDER, None):
            s = sources(h)
            assert ("page", pp_) in s and ("page", hp) not in s
        assert ("page", hp) in sources(H_ADMIN)

        access.add_space_member(conn, ps, 3, added_by=2)
        assert ("page", hp) in sources(H_OUTSIDER)
