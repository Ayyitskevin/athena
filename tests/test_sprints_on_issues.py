"""Sprints where issues actually live: assign/clear a sprint from the issue detail
page, and filter the issue list by sprint.

Both ride the existing sprint data layer and the one shared list_issues filtering
path — so the browser, the REST issue list, and the sprint membership column can
never disagree. These pin the detail-page form (assign, clear, the cross-project
400, and the creator-or-assignee gate) and the list/API sprint filter.
"""
from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # the bootstrap admin (project + issue creator)


def _login(client, email="a@e.com", password="pw"):
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _project(client, name="Ops", key="OPS"):
    r = client.post("/projects", json={"name": name, "key": key}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sprint(client, pid, name="Sprint 1"):
    r = client.post(f"/projects/{pid}/sprints", json={"name": name}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _issue(client, pid, title="Build the thing"):
    r = client.post("/issues", json={"title": title, "project_id": pid}, headers=H1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sprint_of(client, iid):
    return client.get(f"/issues/{iid}", headers=H1).json()["sprint_id"]


def test_assign_and_clear_sprint_from_detail(tmp_path):
    with TestClient(create_app(tmp_path / "assign.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = _project(client)
        sid = _sprint(client, pid)
        iid = _issue(client, pid)
        _login(client)

        # The detail page offers the sprint form and shows the issue as Backlog.
        page = client.get(f"/aegis/issues/{iid}")
        assert page.status_code == 200 and "Set Sprint" in page.text and "Backlog" in page.text

        # Assign it to the sprint.
        moved = client.post(
            f"/aegis/issues/{iid}/sprint", data={"sprint_id": sid}, follow_redirects=False
        )
        assert moved.status_code == 303
        assert _sprint_of(client, iid) == sid
        assert "Sprint 1" in client.get(f"/aegis/issues/{iid}").text

        # Clear it (empty value) → back to the backlog.
        cleared = client.post(
            f"/aegis/issues/{iid}/sprint", data={"sprint_id": ""}, follow_redirects=False
        )
        assert cleared.status_code == 303
        assert _sprint_of(client, iid) is None


def test_cross_project_sprint_is_rejected(tmp_path):
    with TestClient(create_app(tmp_path / "cross.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid_a = _project(client, "A", "AAA")
        pid_b = _project(client, "B", "BBB")
        sid_b = _sprint(client, pid_b)  # a sprint in the OTHER project
        iid = _issue(client, pid_a)
        _login(client)

        # An issue can only join a sprint in its own project — the web 400 mirrors
        # the REST 422 the PUT /issues/{id}/sprint endpoint raises.
        denied = client.post(
            f"/aegis/issues/{iid}/sprint", data={"sprint_id": sid_b}, follow_redirects=False
        )
        assert denied.status_code == 400
        assert _sprint_of(client, iid) is None


def test_sprint_assign_is_author_gated(tmp_path):
    with TestClient(create_app(tmp_path / "gate.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})  # creator
        client.post(
            "/users",
            json={"email": "b@e.com", "name": "B", "password": "pw", "role": "member"},
            headers=H1,
        )
        pid = _project(client)
        sid = _sprint(client, pid)
        iid = _issue(client, pid)  # created by user 1

        _login(client, "b@e.com", "pw")  # a member who is neither creator nor assignee
        denied = client.post(
            f"/aegis/issues/{iid}/sprint", data={"sprint_id": sid}, follow_redirects=False
        )
        assert denied.status_code == 403
        assert _sprint_of(client, iid) is None


def test_no_sprint_form_when_project_has_no_sprints(tmp_path):
    with TestClient(create_app(tmp_path / "empty.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = _project(client)
        iid = _issue(client, pid)  # project exists but has no sprints yet
        _login(client)

        page = client.get(f"/aegis/issues/{iid}")
        # No sprints to join → no form, but the read-only Sprint row still reads Backlog.
        assert page.status_code == 200
        assert "Set Sprint" not in page.text
        assert "Backlog" in page.text


def test_issue_list_filtered_by_sprint(tmp_path):
    with TestClient(create_app(tmp_path / "filter.db")) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        pid = _project(client)
        sid = _sprint(client, pid)
        in_sprint = _issue(client, pid, "In the sprint")
        _issue(client, pid, "Still in backlog")  # stays in the backlog
        client.put(f"/issues/{in_sprint}/sprint", json={"sprint_id": sid}, headers=H1)

        # The REST list and the web list share one filtering path — both narrow to
        # the sprint's issues only.
        api = client.get(f"/issues?sprint={sid}", headers=H1).json()
        assert [i["id"] for i in api] == [in_sprint]

        page = client.get(f"/aegis/issues?sprint={sid}")
        assert page.status_code == 200
        assert "In the sprint" in page.text and "Still in backlog" not in page.text
        # The filter dropdown labels each sprint by its project key.
        assert "OPS · Sprint 1" in page.text
