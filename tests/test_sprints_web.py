"""The browser sprint-management page (/aegis/projects/{id}/sprints).

A thin client over the sprint data layer: reading a project's sprints is open, but
creating and running them is the project creator's call. These pin the page render,
the create form, the start/complete lifecycle (incl. the 409 on an illegal move),
the membership-guarded delete, and the creator-only gating.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _login(client, email="a@e.com", password="pw"):
    r = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _project(client, name="Ops", key="OPS"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()[
        "id"
    ]


def _sprints(client, pid):
    return client.get(f"/projects/{pid}/sprints").json()


def test_page_renders_and_web_create(tmp_path):
    with TestClient(create_app(tmp_path / "w.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = _project(client)
        _login(client)

        page = client.get(f"/aegis/projects/{pid}/sprints")
        assert (
            page.status_code == 200
            and "Sprints" in page.text
            and "New sprint" in page.text
        )

        created = client.post(
            f"/aegis/projects/{pid}/sprints",
            data={"name": "Sprint 1", "goal": "ship"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert [s["name"] for s in _sprints(client, pid)] == ["Sprint 1"]
        assert "Sprint 1" in client.get(f"/aegis/projects/{pid}/sprints").text


def test_start_then_complete_lifecycle(tmp_path):
    with TestClient(create_app(tmp_path / "life.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = _project(client)
        _login(client)
        client.post(
            f"/aegis/projects/{pid}/sprints",
            data={"name": "S1"},
            follow_redirects=False,
        )
        sid = _sprints(client, pid)[0]["id"]

        assert (
            client.post(
                f"/aegis/sprints/{sid}/start", follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            'data-tone="info">active</span>'
            in client.get(f"/aegis/projects/{pid}/sprints").text
        )
        # Starting an active sprint is an illegal move → 409.
        assert (
            client.post(
                f"/aegis/sprints/{sid}/start", follow_redirects=False
            ).status_code
            == 409
        )

        assert (
            client.post(
                f"/aegis/sprints/{sid}/complete", follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            'data-tone="success">completed</span>'
            in client.get(f"/aegis/projects/{pid}/sprints").text
        )


def test_delete_guarded_by_membership(tmp_path):
    with TestClient(create_app(tmp_path / "del.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = _project(client)
        _login(client)
        client.post(
            f"/aegis/projects/{pid}/sprints",
            data={"name": "S1"},
            follow_redirects=False,
        )
        sid = _sprints(client, pid)[0]["id"]
        iss = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()
        client.put(f"/issues/{iss['id']}/sprint", json={"sprint_id": sid}, headers=H1)

        # Holds an issue → refuse.
        assert (
            client.post(
                f"/aegis/sprints/{sid}/delete", follow_redirects=False
            ).status_code
            == 409
        )
        # Empty it → deletes.
        client.put(f"/issues/{iss['id']}/sprint", json={"sprint_id": None}, headers=H1)
        assert (
            client.post(
                f"/aegis/sprints/{sid}/delete", follow_redirects=False
            ).status_code
            == 303
        )
        assert _sprints(client, pid) == []


def test_management_is_creator_only(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        client.post(
            "/users", json={"email": "a@e.com", "name": "A", "password": "pw"}
        )  # creator
        client.post(
            "/users",
            json={"email": "b@e.com", "name": "B", "password": "pw", "role": "member"},
            headers=H1,
        )
        pid = _project(client)  # created by user 1

        _login(client, "b@e.com", "pw")  # a non-creator member
        page = client.get(f"/aegis/projects/{pid}/sprints")
        # Reads, but sees no create form and gets a clear note.
        assert page.status_code == 200
        assert "New sprint" not in page.text
        assert "Only the project creator" in page.text
        # And can't create.
        denied = client.post(
            f"/aegis/projects/{pid}/sprints", data={"name": "X"}, follow_redirects=False
        )
        assert denied.status_code == 403


def test_unknown_project_and_sprint(tmp_path):
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        _login(client)
        assert client.get("/aegis/projects/9999/sprints").status_code == 404
        assert (
            client.post("/aegis/sprints/9999/start", follow_redirects=False).status_code
            == 404
        )
