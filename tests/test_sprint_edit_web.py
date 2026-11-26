"""Editing a sprint from the cockpit (decision 8, T3).

The cockpit created, started, completed and deleted sprints but could not edit
one, so an operator who mistyped a sprint's name, goal or dates had to leave the
browser for ``PATCH /sprints/{id}``. ``POST /aegis/sprints/{id}/edit`` is the
same command, ``sprint_commands.update_sprint``, behind the same gate as start,
complete and delete: the project creator only, a hidden project reads as a
missing one, CSRF-checked, and a 303 back to the sprint list. The command
records ``sprint_edited`` when something changed and nothing for a no-op.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from support import (
    ADMIN,
    CREATOR,
    bootstrap_admin_creator_outsider,
    events,
    login,
    make_app,
)


def _sprint_world(client: TestClient, *, visibility: str = "public") -> tuple:
    """The access cast, a project the creator made, and one planned sprint."""
    bootstrap_admin_creator_outsider(client)
    project = client.post(
        "/projects",
        json={"name": "Ops", "key": "OPS", "visibility": visibility},
        headers=CREATOR,
    ).json()
    sprint = client.post(
        f"/projects/{project['id']}/sprints",
        json={
            "name": "Sprnt 1",
            "goal": "shp it",
            "start_date": "2026-12-01",
            "end_date": "2026-12-14",
        },
        headers=CREATOR,
    )
    assert sprint.status_code == 201, sprint.text
    return project, sprint.json()


def _read(client: TestClient, sprint_id: int) -> dict:
    return client.get(f"/sprints/{sprint_id}", headers=ADMIN).json()


def _edit(client: TestClient, sprint_id: int, **fields):
    form = {
        "name": "Sprint 1",
        "goal": "ship it",
        "start_date": "2026-12-01",
        "end_date": "2026-12-14",
        **fields,
    }
    return client.post(
        f"/aegis/sprints/{sprint_id}/edit", data=form, follow_redirects=False
    )


def test_the_creator_edits_a_sprint_and_lands_back_on_the_list(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        project, sprint = _sprint_world(client)
        login(client, "c@e.com")

        page = client.get(f"/aegis/projects/{project['id']}/sprints")
        assert f'action="/aegis/sprints/{sprint["id"]}/edit"' in page.text
        # The form starts from what the sprint holds.
        assert 'value="Sprnt 1"' in page.text

        edited = _edit(client, sprint["id"], end_date="")
        assert edited.status_code == 303, edited.text
        assert edited.headers["location"] == f"/aegis/projects/{project['id']}/sprints"
        after = _read(client, sprint["id"])
        assert (after["name"], after["goal"]) == ("Sprint 1", "ship it")
        assert (after["start_date"], after["end_date"]) == ("2026-12-01", None)
        assert after["state"] == "planned"
        assert "Sprint 1" in client.get(f"/aegis/projects/{project['id']}/sprints").text

        # The same values again change nothing and record nothing.
        again = _edit(client, sprint["id"], end_date="")
        assert again.status_code == 303
    edits = events(db_file, "sprint_edited")
    assert len(edits) == 1
    assert edits[0]["actor_id"] == 2
    assert edits[0]["detail"] == "Sprnt 1 → Sprint 1, goal changed, end_date changed"


def test_an_empty_name_is_refused_and_nothing_changes(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        _, sprint = _sprint_world(client)
        login(client, "c@e.com")
        refused = _edit(client, sprint["id"], name="   ")
        assert refused.status_code == 400, refused.text
        assert _read(client, sprint["id"])["name"] == "Sprnt 1"
    assert events(db_file, "sprint_edited") == []


def test_only_the_project_creator_may_edit_and_hidden_reads_as_missing(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        project, sprint = _sprint_world(client)
        login(client, "o@e.com")
        page = client.get(f"/aegis/projects/{project['id']}/sprints")
        assert page.status_code == 200
        assert "/edit" not in page.text
        refused = _edit(client, sprint["id"])
        assert refused.status_code == 403, refused.text
        assert _read(client, sprint["id"])["name"] == "Sprnt 1"

    app, db_file_private = make_app(tmp_path, "private.db")
    with TestClient(app) as client:
        _, hidden = _sprint_world(client, visibility="private")
        login(client, "o@e.com")
        blind = _edit(client, hidden["id"])
        missing = _edit(client, 10**6)
        assert blind.status_code == missing.status_code == 404
        # The control: the creator edits the same private sprint.
        login(client, "c@e.com")
        assert _edit(client, hidden["id"]).status_code == 303
    assert events(db_file, "sprint_edited") == []
    assert len(events(db_file_private, "sprint_edited")) == 1


def test_edit_needs_a_session_and_the_csrf_token(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        project, sprint = _sprint_world(client)
        login(client, "c@e.com")
        forged = client.post(
            f"/aegis/sprints/{sprint['id']}/edit",
            data={"name": "Forged"},
            headers={"X-CSRF-Token": "forged"},
            follow_redirects=False,
        )
        assert forged.status_code == 403, forged.text
    with TestClient(app) as anonymous:
        signed_out = _edit(anonymous, sprint["id"], name="Anon")
        assert signed_out.status_code in (401, 403), signed_out.text
    with TestClient(app) as client:
        assert _read(client, sprint["id"])["name"] == "Sprnt 1"
    assert events(db_file, "sprint_edited") == []
