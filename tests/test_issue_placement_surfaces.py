"""Public transports for atomic project+sprint issue placement."""

from fastapi.testclient import TestClient

from athena.aegis import issue_commands
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _bootstrap(client: TestClient) -> None:
    response = client.post(
        "/users",
        json={"email": "a@example.com", "name": "A", "password": "secret"},
    )
    assert response.status_code == 201, response.text


def _project(client: TestClient, name: str, key: str) -> int:
    response = client.post("/projects", json={"name": name, "key": key}, headers=H1)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _sprint(client: TestClient, project_id: int, name: str) -> int:
    response = client.post(
        f"/projects/{project_id}/sprints", json={"name": name}, headers=H1
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _issue(client: TestClient, project_id: int, *, status: str | None = None) -> dict:
    payload = {"title": "placed issue", "project_id": project_id}
    if status is not None:
        payload["status"] = status
    response = client.post("/issues", json=payload, headers=H1)
    assert response.status_code == 201, response.text
    return response.json()


def _get_issue(client: TestClient, issue_id: int) -> dict:
    response = client.get(f"/issues/{issue_id}", headers=H1)
    assert response.status_code == 200, response.text
    return response.json()


def _login(client: TestClient, email: str = "a@example.com") -> None:
    response = client.post("/login", data={"email": email, "password": "secret"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["athena_csrf"]


def test_patch_moves_directly_into_destination_project_sprint(tmp_path):
    # WHY: clients need one audited command, not a project move that first clears
    # the sprint followed by a second request that can fail.
    with TestClient(create_app(tmp_path / "paired-patch.db")) as client:
        _bootstrap(client)
        source = _project(client, "Source", "SRC")
        destination = _project(client, "Destination", "DST")
        source_sprint = _sprint(client, source, "Source sprint")
        destination_sprint = _sprint(client, destination, "Destination sprint")
        issue = _issue(client, source)
        assert (
            client.put(
                f"/issues/{issue['id']}/sprint",
                json={"sprint_id": source_sprint},
                headers=H1,
            ).status_code
            == 200
        )

        response = client.patch(
            f"/issues/{issue['id']}",
            json={
                "project_id": destination,
                "sprint_id": destination_sprint,
            },
            headers=H1,
        )

        assert response.status_code == 200, response.text
        assert (
            response.json()["project_id"],
            response.json()["sprint_id"],
        ) == (destination, destination_sprint)

        # Omitting both relationships on a later edit leaves them untouched.
        unchanged = client.patch(
            f"/issues/{issue['id']}", json={"priority": "high"}, headers=H1
        ).json()
        assert (unchanged["project_id"], unchanged["sprint_id"]) == (
            destination,
            destination_sprint,
        )
        # Explicit null remains distinct from omission and clears both.
        cleared = client.patch(
            f"/issues/{issue['id']}",
            json={"project_id": None, "sprint_id": None},
            headers=H1,
        ).json()
        assert (cleared["project_id"], cleared["sprint_id"]) == (None, None)


def test_patch_rejects_invalid_pair_without_partial_write(tmp_path):
    # WHY: validating the final pair before persistence prevents a failed move from
    # changing the project, priority, key, or sprint independently.
    with TestClient(create_app(tmp_path / "invalid-pair.db")) as client:
        _bootstrap(client)
        source = _project(client, "Source", "SRC")
        destination = _project(client, "Destination", "DST")
        source_sprint = _sprint(client, source, "Source sprint")
        issue = _issue(client, source)
        client.put(
            f"/issues/{issue['id']}/sprint",
            json={"sprint_id": source_sprint},
            headers=H1,
        )

        response = client.patch(
            f"/issues/{issue['id']}",
            json={
                "project_id": destination,
                "sprint_id": source_sprint,
                "priority": "urgent",
            },
            headers=H1,
        )

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "sprint belongs to a different project than the issue"
        )
        after = _get_issue(client, issue["id"])
        assert (after["project_id"], after["sprint_id"], after["priority"]) == (
            source,
            source_sprint,
            "medium",
        )


def test_patch_project_only_keeps_automatic_clear_and_status_remap(tmp_path):
    # WHY: adding paired placement must not change the established project-only
    # behavior: incompatible sprint clears and invalid destination status remaps.
    with TestClient(create_app(tmp_path / "project-only.db")) as client:
        _bootstrap(client)
        source = _project(client, "Source", "SRC")
        destination = _project(client, "Destination", "DST")
        added = client.post(
            f"/projects/{source}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        assert added.status_code == 201, added.text
        source_sprint = _sprint(client, source, "Source sprint")
        issue = _issue(client, source, status="review")
        client.put(
            f"/issues/{issue['id']}/sprint",
            json={"sprint_id": source_sprint},
            headers=H1,
        )

        response = client.patch(
            f"/issues/{issue['id']}",
            json={"project_id": destination},
            headers=H1,
        )

        assert response.status_code == 200, response.text
        assert (
            response.json()["project_id"],
            response.json()["sprint_id"],
            response.json()["status"],
        ) == (destination, None, "open")


def test_bulk_accepts_paired_placement_per_best_effort_item(tmp_path):
    with TestClient(create_app(tmp_path / "paired-bulk.db")) as client:
        _bootstrap(client)
        source = _project(client, "Source", "SRC")
        destination = _project(client, "Destination", "DST")
        destination_sprint = _sprint(client, destination, "Destination sprint")
        issue = _issue(client, source)

        response = client.post(
            "/issues/bulk",
            json={
                "ids": [issue["id"], 9999],
                "project_id": destination,
                "sprint_id": destination_sprint,
            },
            headers=H1,
        )

        assert response.status_code == 200, response.text
        assert (response.json()["updated"], response.json()["failed"]) == (1, 1)
        assert response.json()["results"][1] == {
            "id": 9999,
            "ok": False,
            "error": "no such issue",
        }
        moved = _get_issue(client, issue["id"])
        assert (moved["project_id"], moved["sprint_id"]) == (
            destination,
            destination_sprint,
        )


def test_web_placement_form_calls_one_paired_command(tmp_path, monkeypatch):
    # WHY: the browser is a transport adapter over the same paired command and must
    # show destination sprints without staging two independently committable writes.
    with TestClient(create_app(tmp_path / "paired-web.db")) as client:
        _bootstrap(client)
        source = _project(client, "Source", "SRC")
        destination = _project(client, "Destination", "DST")
        destination_sprint = _sprint(client, destination, "Destination sprint")
        issue = _issue(client, source)
        _login(client)

        page = client.get(f"/aegis/issues/{issue['id']}")
        assert page.status_code == 200
        assert f'action="/aegis/issues/{issue["id"]}/placement"' in page.text
        assert "Destination — Destination sprint" in page.text

        real_update = issue_commands.update_issue
        calls: list[dict] = []

        def recording_update(*args, **kwargs):
            calls.append(kwargs.copy())
            return real_update(*args, **kwargs)

        monkeypatch.setattr(issue_commands, "update_issue", recording_update)
        response = client.post(
            f"/aegis/issues/{issue['id']}/placement",
            data={
                "project_id": str(destination),
                "sprint_id": str(destination_sprint),
            },
            follow_redirects=False,
        )

        assert response.status_code == 303, response.text
        assert len(calls) == 1
        assert calls[0]["project_id"] == destination
        assert calls[0]["sprint_id"] == destination_sprint
        moved = _get_issue(client, issue["id"])
        assert (moved["project_id"], moved["sprint_id"]) == (
            destination,
            destination_sprint,
        )


def test_web_placement_hides_and_rejects_private_destinations(tmp_path):
    # WHY: adding cross-project sprint choices must not turn the detail page or the
    # new combined write into an existence oracle for private projects.
    with TestClient(create_app(tmp_path / "placement-privacy.db")) as client:
        _bootstrap(client)
        created_user = client.post(
            "/users",
            json={
                "email": "b@example.com",
                "name": "B",
                "password": "secret",
                "role": "member",
            },
            headers=H1,
        )
        assert created_user.status_code == 201, created_user.text
        source = _project(client, "Visible", "VIS")
        private = _project(client, "Private destination", "PRV")
        private_sprint = _sprint(client, private, "Secret sprint")
        hidden = client.put(
            f"/projects/{private}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        assert hidden.status_code == 200, hidden.text
        issue_response = client.post(
            "/issues",
            json={"title": "B owns this", "project_id": source},
            headers=H2,
        )
        assert issue_response.status_code == 201, issue_response.text
        issue = issue_response.json()
        _login(client, "b@example.com")

        page = client.get(f"/aegis/issues/{issue['id']}")
        assert page.status_code == 200
        assert "Private destination" not in page.text
        assert "Secret sprint" not in page.text

        response = client.post(
            f"/aegis/issues/{issue['id']}/placement",
            data={
                "project_id": str(private),
                "sprint_id": str(private_sprint),
            },
        )
        assert response.status_code == 400
        assert "No such project" in response.text
        unchanged = client.get(f"/issues/{issue['id']}", headers=H2).json()
        assert (unchanged["project_id"], unchanged["sprint_id"]) == (source, None)
