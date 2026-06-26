"""Issue hierarchy — nesting an issue under a parent, with rollups.

These encode the contract: a parent can be set/cleared and is reported on the
issue and via a children endpoint; an issue can't be its own parent and cycles are
rejected; parent changes are audited and write-gated; and the detail page shows the
parent link, the children, and a done-rollup.
"""
from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def _user2(client):
    client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)


def _issue(client, title):
    return client.post("/issues", json={"title": title}, headers=H1).json()


# --- API --------------------------------------------------------------------


def test_set_get_clear_parent(tmp_path):
    with TestClient(create_app(tmp_path / "h.db")) as client:
        _admin(client)
        parent = _issue(client, "epic")
        child = _issue(client, "task")
        r = client.put(
            f"/issues/{child['id']}/parent", json={"parent_id": parent["id"]}, headers=H1
        )
        assert r.status_code == 200 and r.json()["parent_id"] == parent["id"]
        kids = client.get(f"/issues/{parent['id']}/children").json()
        assert [k["id"] for k in kids] == [child["id"]]
        # Clear it.
        cleared = client.put(
            f"/issues/{child['id']}/parent", json={"parent_id": None}, headers=H1
        ).json()
        assert cleared["parent_id"] is None
        assert client.get(f"/issues/{parent['id']}/children").json() == []


def test_self_parent_rejected(tmp_path):
    with TestClient(create_app(tmp_path / "s.db")) as client:
        _admin(client)
        iss = _issue(client, "x")
        assert (
            client.put(
                f"/issues/{iss['id']}/parent", json={"parent_id": iss["id"]}, headers=H1
            ).status_code
            == 422
        )


def test_cycle_rejected(tmp_path):
    with TestClient(create_app(tmp_path / "c.db")) as client:
        _admin(client)
        a = _issue(client, "a")
        b = _issue(client, "b")
        client.put(f"/issues/{b['id']}/parent", json={"parent_id": a["id"]}, headers=H1)
        # a under b would close the loop a -> b -> a.
        assert (
            client.put(
                f"/issues/{a['id']}/parent", json={"parent_id": b["id"]}, headers=H1
            ).status_code
            == 422
        )


def test_unknown_parent_rejected(tmp_path):
    with TestClient(create_app(tmp_path / "u.db")) as client:
        _admin(client)
        iss = _issue(client, "x")
        assert (
            client.put(
                f"/issues/{iss['id']}/parent", json={"parent_id": 9999}, headers=H1
            ).status_code
            == 422
        )


def test_parent_change_is_audited(tmp_path):
    with TestClient(create_app(tmp_path / "au.db")) as client:
        _admin(client)
        parent = _issue(client, "epic")
        child = _issue(client, "task")
        client.put(
            f"/issues/{child['id']}/parent", json={"parent_id": parent["id"]}, headers=H1
        )
        acts = client.get(
            f"/activity?target_kind=issue&target_id={child['id']}", headers=H1
        ).json()
        assert any(a["verb"] == "set_parent" for a in acts)
        client.put(f"/issues/{child['id']}/parent", json={"parent_id": None}, headers=H1)
        acts2 = client.get(
            f"/activity?target_kind=issue&target_id={child['id']}", headers=H1
        ).json()
        assert any(a["verb"] == "removed_parent" for a in acts2)


def test_parent_is_write_gated(tmp_path):
    with TestClient(create_app(tmp_path / "g.db")) as client:
        _admin(client)
        _user2(client)
        parent = _issue(client, "epic")
        child = _issue(client, "task")  # created by user1
        # user2 is neither creator nor assignee of the child.
        assert (
            client.put(
                f"/issues/{child['id']}/parent",
                json={"parent_id": parent["id"]},
                headers=H2,
            ).status_code
            == 403
        )


def test_children_404_for_missing_issue(tmp_path):
    with TestClient(create_app(tmp_path / "m.db")) as client:
        _admin(client)
        assert client.get("/issues/9999/children").status_code == 404


# --- web --------------------------------------------------------------------


def _login(client):
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


def test_web_detail_shows_parent_and_rollup(tmp_path):
    with TestClient(create_app(tmp_path / "w.db")) as client:
        _admin(client)
        _login(client)
        parent = _issue(client, "epic")
        c1 = _issue(client, "one")
        c2 = _issue(client, "two")
        for c in (c1, c2):
            client.put(f"/issues/{c['id']}/parent", json={"parent_id": parent["id"]}, headers=H1)
        client.patch(f"/issues/{c1['id']}", json={"status": "done"}, headers=H1)

        page = client.get(f"/aegis/issues/{parent['id']}").text
        assert "Sub-issues" in page
        assert "(1/2 done)" in page  # rollup: one of two children is done
        assert f"/aegis/issues/{c1['id']}" in page
        # The child shows a link back up to its parent.
        child_page = client.get(f"/aegis/issues/{c1['id']}").text
        assert f"/aegis/issues/{parent['id']}" in child_page


def test_web_set_and_clear_parent_form(tmp_path):
    with TestClient(create_app(tmp_path / "wf.db")) as client:
        _admin(client)
        csrf = _login(client)
        parent = _issue(client, "epic")
        child = _issue(client, "task")
        client.post(
            f"/aegis/issues/{child['id']}/parent",
            data={"parent_ref": str(parent["id"])},
            headers={"X-CSRF-Token": csrf},
        )
        assert client.get(f"/issues/{child['id']}").json()["parent_id"] == parent["id"]
        # Blank clears it.
        client.post(
            f"/aegis/issues/{child['id']}/parent",
            data={"parent_ref": ""},
            headers={"X-CSRF-Token": csrf},
        )
        assert client.get(f"/issues/{child['id']}").json()["parent_id"] is None
