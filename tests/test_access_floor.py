"""The Office floor respects project visibility (access control).

`office.build_floor` is the newest project-scoped read and the only one whose gate
no test covered. Deleting its single `access.can_see_project` call
(src/athena/aegis/office.py:165) left the entire suite green while a private
project's issue titles, assignees and lease holders answered an outsider — the
exact "a list of routes is what a future endpoint forgets to join" failure the
sibling access tests were written to prevent.

Three read surfaces share that one gate, so all three are pinned here: the REST
floor (`GET /projects/{id}/floor`), the branch-office page
(`GET /aegis/projects/{id}/floor`), and the `floor.json` that page polls. The web
pair resolve identity from the SESSION — `request.state.user`, never
`X-Athena-Actor` — so they are exercised with a real session cookie rather than
the actor header the REST half uses.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.core import db, sessions
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}
BROWSER = {"Accept": "text/html"}
SECRET = "Rotate the production signing key"


def _bootstrap(client):
    for email, name, role in (
        ("admin@e.com", "Admin", "admin"),
        ("c@e.com", "Creator", "member"),
        ("o@e.com", "Outsider", "member"),
    ):
        client.post(
            "/users",
            json={"email": email, "name": name, "password": "pw", "role": role},
            headers=H_ADMIN,
        )


def _private_project_with_an_issue(client, db_file):
    """A private project holding one issue whose title must never leak."""
    project_id = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    client.post(
        "/issues",
        json={"title": SECRET, "project_id": project_id},
        headers=H_CREATOR,
    )
    conn = db.connect(db_file)
    try:
        conn.execute(
            "UPDATE projects SET visibility = 'private' WHERE id = ?", (project_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return project_id


def _session_for(client, db_file, user_id):
    """Sign this client in as a user id. The web floor reads the session, so an
    actor header would leave it anonymous and pass for the wrong reason."""
    conn = db.connect(db_file)
    try:
        raw = sessions.create_session(conn, user_id)
    finally:
        conn.close()
    client.cookies.set("athena_session", raw)


def test_rest_floor_is_hidden_from_an_outsider(tmp_path):
    """The surface the gate deletion exposed: an authenticated member who cannot
    see the project must get the same 404 as a stranger — and must not be told the
    project exists by a different status code."""
    db_file = tmp_path / "floor_rest.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap(client)
        project_id = _private_project_with_an_issue(client, db_file)

        refused = client.get(f"/projects/{project_id}/floor", headers=H_OUTSIDER)
        assert refused.status_code == 404, refused.text
        assert SECRET not in refused.text


def test_rest_floor_still_answers_the_people_who_may_see_it(tmp_path):
    """The other half: the gate must not close the floor to its own project. A
    test that only asserted 404s would pass with build_floor returning None to
    everyone."""
    db_file = tmp_path / "floor_allowed.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap(client)
        project_id = _private_project_with_an_issue(client, db_file)

        for headers in (H_CREATOR, H_ADMIN):
            allowed = client.get(f"/projects/{project_id}/floor", headers=headers)
            assert allowed.status_code == 200, allowed.text
            assert SECRET in allowed.text


def test_web_floor_and_floor_json_are_hidden_from_an_outsider(tmp_path):
    """Both browser surfaces, signed in as a member who cannot see the project.
    floor.json is polled by the page, so a gate on the page alone would still leak
    through the refresh."""
    db_file = tmp_path / "floor_web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap(client)
        project_id = _private_project_with_an_issue(client, db_file)
        _session_for(client, db_file, 3)

        page = client.get(f"/aegis/projects/{project_id}/floor", headers=BROWSER)
        assert page.status_code == 404, page.status_code
        assert SECRET not in page.text

        polled = client.get(f"/aegis/projects/{project_id}/floor.json")
        assert polled.status_code == 404, polled.status_code
        assert SECRET not in polled.text


def test_web_floor_answers_a_member_who_may_see_it(tmp_path):
    """Signed in as the creator, the same two surfaces render — so the refusals
    above are the gate working, not the floor being broken for everyone."""
    db_file = tmp_path / "floor_web_ok.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap(client)
        project_id = _private_project_with_an_issue(client, db_file)
        _session_for(client, db_file, 2)

        page = client.get(f"/aegis/projects/{project_id}/floor", headers=BROWSER)
        assert page.status_code == 200, page.status_code
        assert SECRET in page.text

        polled = client.get(f"/aegis/projects/{project_id}/floor.json")
        assert polled.status_code == 200, polled.status_code
        assert SECRET in polled.text
