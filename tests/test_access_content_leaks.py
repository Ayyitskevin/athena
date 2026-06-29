"""Content-read leaks found by the access-control audit (group 1 of the fixes).

Three places leaked the actual content of a hidden issue/page to someone who can't see
it: (1) the attachment download endpoint served a private issue's/page's file bytes by
id with NO visibility check; (2) the typed-dependency link summaries (blocks/relates,
and the open-blocker close warning) rendered a linked hidden issue's key/title/status;
(3) the issue-detail page rendered a hidden PARENT issue's key/title. All now gate by
the container's visibility, returning the same 404 / omission a missing target gets.
"""
from fastapi.testclient import TestClient

from athena.aegis import dependencies, issues
from athena.core import access, db, users
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


# --- Attachment download / delete ------------------------------------------


def test_issue_attachment_download_gated(tmp_path):
    db_file = tmp_path / "ia.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        iid = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        aid = client.post(
            f"/issues/{iid}/attachments",
            files={"file": ("secret.txt", b"top secret bytes", "text/plain")},
            headers=H_CREATOR,
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.commit()

        url = f"/attachments/{aid}"
        # The bytes are hidden from the outsider and the signed-out caller (same 404 as
        # a missing attachment) — and delete is gated the same way.
        assert client.get(url, headers=H_OUTSIDER).status_code == 404
        assert client.get(url).status_code == 404
        assert client.delete(url, headers=H_OUTSIDER).status_code == 404
        # Open to the creator and admin.
        r = client.get(url, headers=H_CREATOR)
        assert r.status_code == 200 and r.content == b"top secret bytes"
        assert client.get(url, headers=H_ADMIN).status_code == 200
        # Membership opens it.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert client.get(url, headers=H_OUTSIDER).status_code == 200


def test_page_attachment_download_gated(tmp_path):
    db_file = tmp_path / "pa.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
        pid = client.post(f"/spaces/{sid}/pages", json={"title": "P"}, headers=H_CREATOR).json()["id"]
        aid = client.post(
            f"/pages/{pid}/attachments",
            files={"file": ("doc.txt", b"private page file", "text/plain")},
            headers=H_CREATOR,
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility='private' WHERE id=?", (sid,))
        conn.commit()

        url = f"/attachments/{aid}"
        assert client.get(url, headers=H_OUTSIDER).status_code == 404
        assert client.get(url).status_code == 404
        assert client.get(url, headers=H_CREATOR).status_code == 200


def test_public_attachment_still_open(tmp_path):
    db_file = tmp_path / "pub.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        iid = client.post("/issues", json={"title": "Loose"}, headers=H_CREATOR).json()["id"]  # backlog
        aid = client.post(
            f"/issues/{iid}/attachments",
            files={"file": ("a.txt", b"public", "text/plain")},
            headers=H_CREATOR,
        ).json()["id"]
        # A backlog (public) attachment downloads anonymously, unchanged.
        assert client.get(f"/attachments/{aid}").status_code == 200


# --- Typed dependency links ------------------------------------------------


def test_dependency_link_summary_hides_private_target(tmp_path):
    db_file = tmp_path / "dl.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        hidden = client.post("/issues", json={"title": "SecretBlocker", "project_id": pp}, headers=H_CREATOR).json()["id"]
        host = client.post("/issues", json={"title": "Host"}, headers=H_CREATOR).json()["id"]  # backlog, public
        # The creator (who can see both) records that the public host is blocked BY the
        # hidden issue (so the hidden one is an open blocker of host).
        client.post(f"/issues/{host}/links", json={"relation": "blocked_by", "target_ref": str(hidden)}, headers=H_CREATOR)
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.commit()

        # REST: the outsider reading the public host's links does NOT see the hidden
        # target's id/title in the blocked_by list; the creator does.
        out = client.get(f"/issues/{host}/links", headers=H_OUTSIDER).json()
        assert [b["id"] for b in out["blocked_by"]] == []
        mine = client.get(f"/issues/{host}/links", headers=H_CREATOR).json()
        assert hidden in [b["id"] for b in mine["blocked_by"]]

        # Web detail of the public host: the hidden blocker's title is absent for the
        # outsider (anonymous), present for the signed-in creator.
        anon_html = client.get(f"/aegis/issues/{host}").text
        assert "SecretBlocker" not in anon_html

        # Data layer: open_blockers filters by the viewer too.
        creator, outsider = users.get_user(conn, 2), users.get_user(conn, 3)
        # hidden blocks host, and hidden is not done → it's an open blocker of host.
        assert hidden in [b["id"] for b in dependencies.open_blockers(conn, host, actor=creator)]
        assert dependencies.open_blockers(conn, host, actor=outsider) == []


def test_issue_detail_hides_private_parent(tmp_path):
    db_file = tmp_path / "pp.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        parent = client.post("/issues", json={"title": "SecretParent", "project_id": pp}, headers=H_CREATOR).json()["id"]
        child = client.post("/issues", json={"title": "Child"}, headers=H_CREATOR).json()["id"]  # backlog, public
        conn = db.connect(db_file)
        issues.set_parent(conn, child, parent)
        conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pp,))
        conn.commit()

        # The public child is viewable by the outsider, but its hidden parent's title
        # must not render; the creator (who can see the parent) does see it.
        anon = client.get(f"/aegis/issues/{child}").text
        assert "SecretParent" not in anon
