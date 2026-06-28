"""Mentor page sub-resource reads respect space visibility (slice 5 hardening).

Slice 3 gated the page itself (show_page / list_pages) but left the per-page reads —
comments, attachments, version list, a single version snapshot, and backlinks — gated
only on page *existence*. A version snapshot IS the page's body, so once a space is
flipped private (slice 5 makes that possible) an outsider could read a hidden page's
content and discussion through these. They now all funnel through _page_for_read and
404 for anyone who can't see the space — the Mentor twin of the issue sub-resource gate.
"""
import pytest
from fastapi.testclient import TestClient

from athena.core import access, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}

LIST_SUBRESOURCES = ["comments", "attachments", "versions", "backlinks"]


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


def _private_page(client, db_file):
    sid = client.post("/spaces", json={"key": "SEC", "name": "Secret"}, headers=H_CREATOR).json()["id"]
    pid = client.post(f"/spaces/{sid}/pages", json={"title": "Hidden", "body": "v1"}, headers=H_CREATOR).json()["id"]
    # Seed a comment + an edit so the comments and versions endpoints would carry real
    # content if they weren't gated.
    client.post(f"/pages/{pid}/comments", json={"body": "secret note"}, headers=H_CREATOR)
    client.patch(f"/pages/{pid}", json={"body": "v2"}, headers=H_CREATOR)
    conn = db.connect(db_file)
    conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (sid,))
    conn.commit()
    return sid, pid


@pytest.mark.parametrize("sub", LIST_SUBRESOURCES)
def test_page_subresource_404s_for_outsider_on_private_page(tmp_path, sub):
    db_file = tmp_path / f"psub_{sub}.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid, pid = _private_page(client, db_file)
        url = f"/pages/{pid}/{sub}"
        # Hidden from the outsider and the signed-out caller...
        assert client.get(url, headers=H_OUTSIDER).status_code == 404
        assert client.get(url).status_code == 404
        # ...open to the creator and admin.
        assert client.get(url, headers=H_CREATOR).status_code == 200
        assert client.get(url, headers=H_ADMIN).status_code == 200
        # Membership opens it up.
        access.add_space_member(db.connect(db_file), sid, 3, added_by=2)
        assert client.get(url, headers=H_OUTSIDER).status_code == 200


def test_page_version_snapshot_gated(tmp_path):
    db_file = tmp_path / "pver.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid, pid = _private_page(client, db_file)
        # A real version exists (the edit snapshotted v1); read its number as the creator.
        versions = client.get(f"/pages/{pid}/versions", headers=H_CREATOR).json()
        assert versions, "expected at least one version after an edit"
        vnum = versions[0]["version"]
        url = f"/pages/{pid}/versions/{vnum}"
        # The historical body is the page's content — hidden from outsiders, open to the
        # creator (who can read the old "v1" text).
        assert client.get(url, headers=H_OUTSIDER).status_code == 404
        assert client.get(url).status_code == 404
        r = client.get(url, headers=H_CREATOR)
        assert r.status_code == 200 and r.json()["body"] == "v1"


def test_public_page_subresources_stay_open(tmp_path):
    db_file = tmp_path / "ppub.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        sid = client.post("/spaces", json={"key": "OPN", "name": "Open"}, headers=H_CREATOR).json()["id"]
        pid = client.post(f"/spaces/{sid}/pages", json={"title": "Open page"}, headers=H_CREATOR).json()["id"]
        # A page in a public space reads like before — every sub-resource is open to all.
        for sub in LIST_SUBRESOURCES:
            assert client.get(f"/pages/{pid}/{sub}").status_code == 200
