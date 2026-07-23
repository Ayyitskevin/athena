"""Page labels — attach the shared label vocabulary to Mentor pages.

A page carries the SAME labels an issue does (the vocabulary now lives in core), so
this is the page twin of issue labels. These pin the REST attach/detach (idempotent,
gated, recorded), labels riding on PageOut, and the web chip row + add-by-name form.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})


def _space(client):
    return client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()["id"]


def _page(client, sid, title="Doc"):
    return client.post(
        f"/spaces/{sid}/pages", json={"title": title}, headers=H1
    ).json()["id"]


def _label(client, name="runbook", color="#ff0000"):
    return client.post(
        "/labels", json={"name": name, "color": color}, headers=H1
    ).json()["id"]


# --- REST API --------------------------------------------------------------


def test_attach_detach_and_labels_on_pageout(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        lid = _label(client)

        attached = client.post(
            f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1
        )
        assert attached.status_code == 201
        assert [la["name"] for la in attached.json()["labels"]] == ["runbook"]
        # The label rides on the page's own GET, and on the space's page list.
        assert [la["id"] for la in client.get(f"/pages/{pid}").json()["labels"]] == [
            lid
        ]
        listed = client.get(f"/spaces/{_space_of(client, pid)}/pages").json()
        assert [la["id"] for page in listed for la in page["labels"]] == [lid]

        # Idempotent re-attach keeps one.
        assert (
            len(
                client.post(
                    f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1
                ).json()["labels"]
            )
            == 1
        )

        detached = client.delete(f"/pages/{pid}/labels/{lid}", headers=H1)
        assert detached.status_code == 200 and detached.json()["labels"] == []
        # Detaching a label that isn't there is a 404.
        assert (
            client.delete(f"/pages/{pid}/labels/{lid}", headers=H1).status_code == 404
        )


def _space_of(client, page_id):
    return client.get(f"/pages/{page_id}").json()["space_id"]


def test_attach_guards(tmp_path):
    with TestClient(create_app(tmp_path / "guards.db")) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        # A bogus label id is a 422 (not an FK 500); an unknown page is a 404.
        assert (
            client.post(
                f"/pages/{pid}/labels", json={"label_id": 9999}, headers=H1
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/pages/9999/labels", json={"label_id": 1}, headers=H1
            ).status_code
            == 404
        )


def test_label_change_is_recorded(tmp_path):
    db_file = tmp_path / "trail.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        lid = _label(client, "bug")
        client.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1)
        client.delete(f"/pages/{pid}/labels/{lid}", headers=H1)
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT verb, detail FROM activity WHERE target_kind='page' AND target_id=? ORDER BY id",
        (pid,),
    ).fetchall()
    conn.close()
    verbs = {r["verb"]: r["detail"] for r in rows}
    # Both lifecycle facts, stamped with the label's name.
    assert verbs.get("page_labeled") == "bug" and verbs.get("page_unlabeled") == "bug"


# --- Web -------------------------------------------------------------------


def _login(client, email="a@e.com", password="pw"):
    client.post("/users", json={"email": email, "name": "A", "password": password})
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_add_by_name_and_remove(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        _login(client)
        pid = _page(client, _space(client))

        page = client.get(f"/mentor/pages/{pid}")
        assert "Labels" in page.text and "Add label" in page.text
        # Add by typing a name (find-or-create) → chip shows on the page.
        assert (
            client.post(
                f"/mentor/pages/{pid}/labels",
                data={"name": "frontend"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert "frontend" in client.get(f"/mentor/pages/{pid}").text

        lid = client.get(f"/pages/{pid}").json()["labels"][0]["id"]
        assert (
            client.post(
                f"/mentor/pages/{pid}/labels/{lid}/delete", follow_redirects=False
            ).status_code
            == 303
        )
        assert client.get(f"/pages/{pid}").json()["labels"] == []
        # Removing from a missing page is a 404 (symmetric with add).
        assert (
            client.post(
                f"/mentor/pages/9999/labels/{lid}/delete", follow_redirects=False
            ).status_code
            == 404
        )


def test_web_label_requires_login(tmp_path):
    with TestClient(create_app(tmp_path / "anon.db")) as client:
        _bootstrap(client)
        pid = _page(client, _space(client))
        anon = TestClient(client.app)
        assert anon.post(
            f"/mentor/pages/{pid}/labels", data={"name": "x"}, follow_redirects=False
        ).status_code in (401, 403)
        assert client.get(f"/pages/{pid}").json()["labels"] == []
