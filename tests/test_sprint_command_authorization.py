"""Sprint commands authorize themselves (G1).

`COMMAND_MIGRATION.md` listed every sprint command as a bare-actor debtor: each
took an ``actor_id`` and trusted the route's project-creator gate, so *the
command owned the write and its audit, not the gate*. A caller reaching the
command directly — a workflow, a script, a future transport — could start,
rename or delete any project's sprint.

These tests call the commands with **no transport in front of them**, the way
``tests/test_comment_command_authorization.py`` does for the comment commands: a
route test cannot tell whether the gate moved, because the route refuses first
either way. The wire contract is pinned at the end, with the transports' own
gate switched off, so what answers there is the command's refusal as each
transport maps it.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient
import pytest

from athena.aegis import projects, sprint_commands, sprints_api
from athena.core import db, users
from athena.web import projects as web_projects
from support import (
    ADMIN,
    CREATOR,
    bootstrap_admin_creator_outsider,
    login,
    make_app,
)

ADMIN_ID, CREATOR_ID, OUTSIDER_ID = 1, 2, 3
VERBS = {
    "create_sprint": sprint_commands.VERB_CREATED,
    "update_sprint": sprint_commands.VERB_EDITED,
    "start_sprint": sprint_commands.VERB_STARTED,
    "complete_sprint": sprint_commands.VERB_COMPLETED,
    "delete_sprint": sprint_commands.VERB_DELETED,
}
COMMANDS = tuple(VERBS)


def _project(client: TestClient, key: str, visibility: str) -> int:
    project = client.post(
        "/projects", json={"key": key, "name": key}, headers=CREATOR
    ).json()
    if visibility == "private":
        assert (
            client.put(
                f"/projects/{project['id']}/visibility",
                json={"visibility": "private"},
                headers=CREATOR,
            ).status_code
            == 200
        )
    return project["id"]


def _sprint(client: TestClient, project_id: int, name: str) -> int:
    response = client.post(
        f"/projects/{project_id}/sprints", json={"name": name}, headers=CREATOR
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture()
def world(tmp_path):
    """For each visibility, two of the creator's projects: one with a planned and
    an active sprint, one with only a planned sprint (so starting it is legal) —
    built over the real API."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap_admin_creator_outsider(client)
        built: dict = {"app": app, "db": db_file}
        for key, visibility in (("PUB", "public"), ("PRV", "private")):
            project = _project(client, key, visibility)
            idle = _project(client, f"{key}I", visibility)
            planned = _sprint(client, project, f"{key} planned")
            active = _sprint(client, project, f"{key} active")
            assert (
                client.post(f"/sprints/{active}/start", headers=CREATOR).status_code
                == 200
            )
            built[visibility] = {
                "project": project,
                "planned": planned,
                "active": active,
                "startable": _sprint(client, idle, f"{key} startable"),
            }
        return built


@pytest.fixture()
def conn(world):
    connection = db.connect(world["db"])
    yield connection
    connection.close()


def _call(conn: sqlite3.Connection, command: str, actor: dict, target: dict):
    """Each command aimed at a sprint it could really change — start one in a
    project with no active sprint, complete the active one — so a missing gate
    would write rather than trip the state machine."""
    if command == "create_sprint":
        return sprint_commands.create_sprint(
            conn, actor=actor, project_id=target["project"], name="Intruder"
        )
    if command == "update_sprint":
        return sprint_commands.update_sprint(
            conn, actor=actor, sprint_id=target["planned"], fields={"name": "Hijacked"}
        )
    sprint_id = target[
        {"start_sprint": "startable", "complete_sprint": "active"}.get(
            command, "planned"
        )
    ]
    return getattr(sprint_commands, command)(conn, actor=actor, sprint_id=sprint_id)


def _state(conn: sqlite3.Connection) -> tuple:
    """Every sprint row and every sprint event: what a refused call must not move."""
    rows = conn.execute("SELECT * FROM sprints ORDER BY id").fetchall()
    trail = conn.execute(
        "SELECT actor_id, verb, target_id, detail FROM activity "
        "WHERE verb LIKE 'sprint_%' ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows], [tuple(row) for row in trail]


# --- the gate now lives in the command ---------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_a_hidden_project_is_refused_by_the_command_as_missing(conn, world, command):
    # WHY: the debt, stated. Before, an outsider reaching the command directly
    # changed a sprint in a private project it cannot even see. Hidden reads as
    # missing, never 403, so the project's existence does not leak either.
    outsider = users.get_user(conn, OUTSIDER_ID)
    before = _state(conn)
    with pytest.raises(sprint_commands.SprintCommandError) as caught:
        _call(conn, command, outsider, world["private"])
    assert (caught.value.kind, str(caught.value)) == ("not_found", "no such project")
    assert _state(conn) == before


@pytest.mark.parametrize("actor_id", [OUTSIDER_ID, ADMIN_ID], ids=["member", "admin"])
@pytest.mark.parametrize("command", COMMANDS)
def test_a_visible_non_creator_is_refused_by_the_command(
    conn, world, command, actor_id
):
    # Managing a project's cadence is the creator's call; an admin who can see the
    # project is not excepted, exactly as the routes have always answered.
    before = _state(conn)
    with pytest.raises(sprint_commands.SprintCommandError) as caught:
        _call(conn, command, users.get_user(conn, actor_id), world["public"])
    assert caught.value.kind == "forbidden"
    assert str(caught.value) == "only the project creator may manage its sprints"
    assert _state(conn) == before


@pytest.mark.parametrize("command", COMMANDS)
def test_the_creator_still_succeeds_directly_and_is_attributed(conn, world, command):
    # Positive control: the gate refuses the wrong actor, not every caller, and the
    # event is attributed to the actor the command was handed.
    _call(conn, command, users.get_user(conn, CREATOR_ID), world["private"])
    trail = _state(conn)[1]
    assert trail[-1][:2] == (CREATOR_ID, VERBS[command])


@pytest.mark.parametrize(
    "command", ["update_sprint", "start_sprint", "complete_sprint"]
)
def test_a_missing_sprint_is_refused_as_missing(conn, command):
    creator = users.get_user(conn, CREATOR_ID)
    with pytest.raises(sprint_commands.SprintCommandError) as caught:
        if command == "update_sprint":
            sprint_commands.update_sprint(
                conn, actor=creator, sprint_id=9999, fields={"name": "x"}
            )
        else:
            getattr(sprint_commands, command)(conn, actor=creator, sprint_id=9999)
    assert (caught.value.kind, str(caught.value)) == ("not_found", "no such sprint")
    # A delete of a sprint that is already gone stays a quiet False, unchanged.
    assert sprint_commands.delete_sprint(conn, actor=creator, sprint_id=9999) is False


def test_a_missing_project_is_refused_as_missing(conn):
    # Before, a direct create on an unknown project reached the foreign key and
    # raised IntegrityError; the gate now answers first, the way the routes do.
    before = _state(conn)
    with pytest.raises(sprint_commands.SprintCommandError) as caught:
        sprint_commands.create_sprint(
            conn, actor=users.get_user(conn, CREATOR_ID), project_id=9999, name="x"
        )
    assert (caught.value.kind, str(caught.value)) == ("not_found", "no such project")
    assert _state(conn) == before


# --- each transport maps the command's own refusal ---------------------------
#
# The transports keep their gate, which answers first. Switch it off and the
# command's in-transaction re-check is what refuses; these pin that each
# transport then answers with the same status the gate it skipped would have.

_REST = {
    "create_sprint": lambda c, t, h: c.post(
        f"/projects/{t['project']}/sprints", json={"name": "Intruder"}, headers=h
    ),
    "update_sprint": lambda c, t, h: c.patch(
        f"/sprints/{t['planned']}", json={"name": "Hijacked"}, headers=h
    ),
    "start_sprint": lambda c, t, h: c.post(
        f"/sprints/{t['startable']}/start", headers=h
    ),
    "complete_sprint": lambda c, t, h: c.post(
        f"/sprints/{t['active']}/complete", headers=h
    ),
    "delete_sprint": lambda c, t, h: c.delete(f"/sprints/{t['planned']}", headers=h),
}


@pytest.mark.parametrize("command", COMMANDS)
def test_rest_answers_the_commands_refusal_with_the_same_status(
    world, monkeypatch, command
):
    monkeypatch.setattr(sprints_api, "_project_for_sprint_write", lambda *a: None)
    with TestClient(world["app"]) as client:
        hidden = _REST[command](client, world["private"], {"X-Athena-Actor": "3"})
        assert (hidden.status_code, hidden.json()["code"]) == (404, "not_found")
        assert hidden.json()["detail"] == "no such project"
        refused = _REST[command](client, world["public"], ADMIN)
        assert (refused.status_code, refused.json()["code"]) == (403, "forbidden")
        assert refused.json()["detail"] == (
            "only the project creator may manage its sprints"
        )


_COCKPIT = {
    "create_sprint": lambda t: f"/aegis/projects/{t['project']}/sprints",
    "start_sprint": lambda t: f"/aegis/sprints/{t['startable']}/start",
    "complete_sprint": lambda t: f"/aegis/sprints/{t['active']}/complete",
    "delete_sprint": lambda t: f"/aegis/sprints/{t['planned']}/delete",
}


@pytest.mark.parametrize("command", sorted(_COCKPIT))
def test_the_cockpit_answers_the_commands_refusal(world, conn, monkeypatch, command):
    # The cockpit has no sprint edit (scripts/surface_parity.toml), so four routes.
    def no_gate(conn, project_id, user):
        return projects.get_project(conn, project_id), None

    monkeypatch.setattr(web_projects, "_authorize_project_write", no_gate)
    before = _state(conn)
    with TestClient(world["app"]) as client:
        login(client, "o@e.com")
        path = _COCKPIT[command](world["public"])
        refused = client.post(path, data={"name": "Intruder"}, follow_redirects=False)
        assert refused.status_code == 403
        assert "Only the project creator may manage its sprints." in refused.text
    assert _state(conn) == before
