"""The sprints REST API: lifecycle, project-creator gating, and issue assignment.

These encode the HTTP contract on top of the sprint data layer: sprints are created
planned and read openly; start/complete enforce the state machine (409 on an illegal
move, incl. a second active sprint); managing a sprint is the project creator's call
(403 otherwise); and an issue can be put in a sprint of ITS OWN project (422 across
projects), recorded on the activity trail.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # the bootstrap admin (project creator)
H2 = {"X-Athena-Actor": "2"}  # a second member


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "sprints_api.db"))


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1
    )


def _project(client, name="Ops", key="OPS"):
    r = client.post("/projects", json={"name": name, "key": key}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sprint(client, pid, name="Sprint 1"):
    r = client.post(f"/projects/{pid}/sprints", json={"name": name}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_list_get_and_state_filter(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)
        s = _sprint(client, pid)
        assert s["state"] == "planned"

        listed = client.get(f"/projects/{pid}/sprints").json()
        assert [x["id"] for x in listed] == [s["id"]]
        assert client.get(f"/sprints/{s['id']}").json()["name"] == "Sprint 1"
        # State filter + unknown state guard.
        assert client.get(f"/projects/{pid}/sprints?state=planned").json()
        assert client.get(f"/projects/{pid}/sprints?state=active").json() == []
        assert client.get(f"/projects/{pid}/sprints?state=bogus").status_code == 422
        assert client.get("/sprints/9999").status_code == 404


def test_lifecycle_start_then_complete_with_guards(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)
        s = _sprint(client, pid)

        started = client.post(f"/sprints/{s['id']}/start", headers=H1)
        assert started.status_code == 200 and started.json()["state"] == "active"
        assert started.json()["start_date"]
        # Can't start an already-active sprint.
        assert client.post(f"/sprints/{s['id']}/start", headers=H1).status_code == 409

        done = client.post(f"/sprints/{s['id']}/complete", headers=H1)
        assert done.status_code == 200 and done.json()["state"] == "completed"
        # Can't complete a completed sprint.
        assert (
            client.post(f"/sprints/{s['id']}/complete", headers=H1).status_code == 409
        )


def test_one_active_sprint_per_project(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)
        a = _sprint(client, pid, "A")
        b = _sprint(client, pid, "B")
        assert client.post(f"/sprints/{a['id']}/start", headers=H1).status_code == 200
        assert client.post(f"/sprints/{b['id']}/start", headers=H1).status_code == 409


def test_management_is_project_creator_only(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)  # created by user 1
        s = _sprint(client, pid)
        # User 2 may READ but not manage.
        assert client.get(f"/projects/{pid}/sprints").status_code == 200
        assert (
            client.post(
                f"/projects/{pid}/sprints", json={"name": "X"}, headers=H2
            ).status_code
            == 403
        )
        assert client.post(f"/sprints/{s['id']}/start", headers=H2).status_code == 403
        assert client.delete(f"/sprints/{s['id']}", headers=H2).status_code == 403


def test_update_and_validation(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)
        s = _sprint(client, pid)
        patched = client.patch(f"/sprints/{s['id']}", json={"goal": "ship"}, headers=H1)
        assert patched.status_code == 200 and patched.json()["goal"] == "ship"
        assert (
            client.patch(f"/sprints/{s['id']}", json={}, headers=H1).status_code == 422
        )
        assert (
            client.patch(
                f"/sprints/{s['id']}", json={"name": "  "}, headers=H1
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/projects/{pid}/sprints", json={"name": ""}, headers=H1
            ).status_code
            == 422
        )


def test_assign_issue_to_sprint_and_backlog(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)
        s = _sprint(client, pid)
        iss = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()

        in_sprint = client.put(
            f"/issues/{iss['id']}/sprint", json={"sprint_id": s["id"]}, headers=H1
        )
        assert in_sprint.status_code == 200 and in_sprint.json()["sprint_id"] == s["id"]
        # Recorded on the trail.
        verbs = {e["verb"] for e in client.get("/activity", headers=H1).json()}
        assert "moved_to_sprint" in verbs

        back = client.put(
            f"/issues/{iss['id']}/sprint", json={"sprint_id": None}, headers=H1
        )
        assert back.status_code == 200 and back.json()["sprint_id"] is None


def test_assign_rejects_unknown_and_cross_project_sprint(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        p1 = _project(client, "One", "ONE")
        p2 = _project(client, "Two", "TWO")
        s2 = _sprint(client, p2, "P2 Sprint")  # a sprint in project 2
        iss = client.post(
            "/issues", json={"title": "x", "project_id": p1}, headers=H1
        ).json()

        # Unknown sprint, and a sprint from another project, are both 422.
        assert (
            client.put(
                f"/issues/{iss['id']}/sprint", json={"sprint_id": 9999}, headers=H1
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/issues/{iss['id']}/sprint", json={"sprint_id": s2["id"]}, headers=H1
            ).status_code
            == 422
        )


def test_delete_guarded_by_membership(tmp_path):
    with _client(tmp_path) as client:
        _bootstrap(client)
        pid = _project(client)
        s = _sprint(client, pid)
        iss = client.post(
            "/issues", json={"title": "x", "project_id": pid}, headers=H1
        ).json()
        client.put(
            f"/issues/{iss['id']}/sprint", json={"sprint_id": s["id"]}, headers=H1
        )

        # Still holds an issue → refuse.
        assert client.delete(f"/sprints/{s['id']}", headers=H1).status_code == 409
        # Empty it, then it deletes.
        client.put(f"/issues/{iss['id']}/sprint", json={"sprint_id": None}, headers=H1)
        assert client.delete(f"/sprints/{s['id']}", headers=H1).status_code == 204
        assert client.delete("/sprints/9999", headers=H1).status_code == 404
