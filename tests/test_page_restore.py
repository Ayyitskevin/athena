"""Restoring a Mentor page to a prior revision.

History was already viewable (list versions, view one); this makes it actionable.
The contract these encode — why each rule earns its place:

  * restore is NON-DESTRUCTIVE: it makes an old revision's content live again by
    cutting a NEW version from the currently-live content first, so nothing is lost
    and you can always restore forward again;
  * the restored page is stamped to whoever restored it (a real edit), while the
    snapshot of the displaced content keeps ITS own author — blame stays honest;
  * restoring content identical to the live row changes nothing and cuts no version;
  * restore IS an edit, so it's open to any signed-in actor (like edit/move), NOT
    creator-locked the way delete is; anonymous -> 401, missing page/version -> 404.
"""
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mentor import pages, spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_two_users(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('ann@e.com', 'Ann')")
    conn.execute("INSERT INTO users (email, name) VALUES ('bob@e.com', 'Bob')")
    conn.commit()
    conn.close()


def _space_with_page(conn, *, title="Runbook", body="v1 text", created_by=1):
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'B')")
    conn.commit()
    space = spaces.create_space(conn, key="ENG", name="Engineering", created_by=created_by)
    return pages.create_page(
        conn, space_id=space["id"], title=title, body=body, created_by=created_by
    )


# --- unit: data access ------------------------------------------------------


def test_restore_makes_prior_content_live_and_keeps_current(tmp_path):
    # WHY: the whole point — the old content comes back as the present, and the
    # content it displaced is preserved as the newest version, not discarded.
    conn = _migrated_conn(tmp_path / "r.db")
    page = _space_with_page(conn, body="v1 text")
    pages.update_page(conn, page["id"], editor_id=1, body="v2 text")  # cuts version 1 (v1)
    pages.update_page(conn, page["id"], editor_id=1, body="v3 text")  # cuts version 2 (v2)

    restored = pages.restore_version(conn, page["id"], 1, editor_id=1)
    assert restored["body"] == "v1 text"  # the old content is live again
    assert pages.get_page(conn, page["id"])["body"] == "v1 text"
    # The displaced live content (v3) is now the newest version; nothing lost.
    history = pages.list_page_versions(conn, page["id"])
    assert [h["version"] for h in history] == [3, 2, 1]
    assert history[0]["body"] == "v3 text"


def test_restore_missing_page_or_version_returns_none(tmp_path):
    conn = _migrated_conn(tmp_path / "rm.db")
    page = _space_with_page(conn)
    assert pages.restore_version(conn, page["id"], 99, editor_id=1) is None  # no such version
    assert pages.restore_version(conn, 999, 1, editor_id=1) is None  # no such page


def test_restore_identical_content_cuts_no_version(tmp_path):
    # WHY: restoring the content the page already shows is a no-op — update_page's
    # no-change rule means we don't litter history with a duplicate snapshot.
    conn = _migrated_conn(tmp_path / "ri.db")
    page = _space_with_page(conn, body="v1 text")
    pages.update_page(conn, page["id"], editor_id=1, body="v2 text")  # version 1 = v1
    pages.restore_version(conn, page["id"], 1, editor_id=1)  # live -> v1, version 2 = v2
    assert len(pages.list_page_versions(conn, page["id"])) == 2
    # restoring v1 again, when v1 is already live, changes nothing
    pages.restore_version(conn, page["id"], 1, editor_id=1)
    assert len(pages.list_page_versions(conn, page["id"])) == 2


def test_restore_attributes_new_revision_to_the_restorer(tmp_path):
    # WHY: a restore is an edit — the live row must be stamped to whoever restored,
    # while the snapshot of the displaced content keeps its own author.
    conn = _migrated_conn(tmp_path / "ra.db")
    page = _space_with_page(conn, body="v1 text", created_by=1)  # user 1 = Ann
    pages.update_page(conn, page["id"], editor_id=1, body="v2 text")  # Ann wrote v2; version1 = v1
    pages.restore_version(conn, page["id"], 1, editor_id=2)  # user 2 = Bob restores v1
    live = pages.get_page(conn, page["id"])
    assert live["body"] == "v1 text"
    assert live["updated_by"] == 2  # the restore is Bob's edit
    # the snapshot of the displaced v2 content stays attributed to Ann, who wrote it
    newest = pages.list_page_versions(conn, page["id"])[0]
    assert newest["body"] == "v2 text"
    assert newest["edited_by"] == 1


# --- API --------------------------------------------------------------------


def _make_space_and_page(client, db_file, *, body="v1 text"):
    _seed_two_users(db_file)
    space = client.post(
        "/spaces", json={"key": "ENG", "name": "Engineering"},
        headers={"X-Athena-Actor": "1"},
    ).json()
    return client.post(
        f"/spaces/{space['id']}/pages",
        json={"title": "Runbook", "body": body},
        headers={"X-Athena-Actor": "1"},
    ).json()


def test_api_restore_returns_updated_page(tmp_path):
    app = create_app(tmp_path / "ar.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "ar.db", body="v1 text")
        client.patch(f"/pages/{page['id']}", json={"body": "v2 text"}, headers={"X-Athena-Actor": "1"})
        r = client.post(f"/pages/{page['id']}/versions/1/restore", headers={"X-Athena-Actor": "2"})
        assert r.status_code == 200, r.text
        assert r.json()["body"] == "v1 text"  # old content live again
        assert r.json()["updated_by"] == 2  # stamped to the restorer
        # the displaced v2 content is preserved as the newest version
        hist = client.get(f"/pages/{page['id']}/versions").json()
        assert hist[0]["body"] == "v2 text"


def test_api_restore_requires_an_actor(tmp_path):
    app = create_app(tmp_path / "arq.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "arq.db")
        client.patch(f"/pages/{page['id']}", json={"body": "v2"}, headers={"X-Athena-Actor": "1"})
        assert client.post(f"/pages/{page['id']}/versions/1/restore").status_code == 401


def test_api_restore_unknown_version_is_404(tmp_path):
    app = create_app(tmp_path / "arv.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "arv.db")
        r = client.post(f"/pages/{page['id']}/versions/7/restore", headers={"X-Athena-Actor": "1"})
        assert r.status_code == 404


def test_api_restore_unknown_page_is_404(tmp_path):
    app = create_app(tmp_path / "arp.db")
    with TestClient(app) as client:
        _seed_two_users(tmp_path / "arp.db")
        r = client.post("/pages/999/versions/1/restore", headers={"X-Athena-Actor": "1"})
        assert r.status_code == 404


# --- web --------------------------------------------------------------------


_H = {"X-Athena-Actor": "1"}


def _login(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "secret"}, headers=_H)
    client.post("/login", data={"email": "a@e.com", "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _logged_in_page_with_history(client):
    """A space + page edited once (so it has version 1), created by the session user."""
    space = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=_H).json()
    page = client.post(
        f"/spaces/{space['id']}/pages", json={"title": "Runbook", "body": "v1 text"}, headers=_H
    ).json()
    client.patch(f"/pages/{page['id']}", json={"body": "v2 text"}, headers=_H)
    return page


def test_web_restore_redirects_and_swaps_content(tmp_path):
    app = create_app(tmp_path / "wr.db")
    with TestClient(app) as client:
        _login(client)
        page = _logged_in_page_with_history(client)
        r = client.post(
            f"/mentor/pages/{page['id']}/versions/1/restore", follow_redirects=False
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/mentor/pages/{page['id']}"
        assert client.get(f"/pages/{page['id']}").json()["body"] == "v1 text"


def test_web_restore_requires_login(tmp_path):
    app = create_app(tmp_path / "wrl.db")
    with TestClient(app) as client:
        _login(client)
        page = _logged_in_page_with_history(client)
    with TestClient(app) as anon:
        assert anon.post(f"/mentor/pages/{page['id']}/versions/1/restore").status_code == 401


def test_web_restore_unknown_version_is_404(tmp_path):
    app = create_app(tmp_path / "wrv.db")
    with TestClient(app) as client:
        _login(client)
        page = _logged_in_page_with_history(client)
        r = client.post(f"/mentor/pages/{page['id']}/versions/9/restore")
        assert r.status_code == 404


def test_web_restore_button_shown_only_when_logged_in(tmp_path):
    # WHY: the Restore control is a write, so an anonymous reader must not see it
    # (the history table itself stays visible — reading is open).
    app = create_app(tmp_path / "wrb.db")
    with TestClient(app) as client:
        _login(client)
        page = _logged_in_page_with_history(client)
        assert "Restore" in client.get(f"/mentor/pages/{page['id']}").text
    with TestClient(app) as anon:
        body = anon.get(f"/mentor/pages/{page['id']}").text
        assert "Version" in body  # history table still renders for readers
        assert "Restore" not in body
