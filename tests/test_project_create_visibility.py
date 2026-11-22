"""A project can be created private, in one audited write (F5, FLEET-16).

``POST /projects`` with ``"visibility": "private"`` used to answer 201 with a
public project: the body model ignored the field, so an agent that asked for a
private container got one every account could read, and nothing told it. The
body now refuses fields it does not declare, and ``visibility`` is declared:
the create command applies it inside its own transaction, through the same
writer, roster row and event the visibility command uses, so the project is
never public for an instant and its narrowing is on the trail.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena import config
from athena.core import db
from support import ADMIN, CREATOR, OUTSIDER, bootstrap_admin_creator_outsider
from support import events, make_app


def _projects(db_file) -> list[tuple[str, str]]:
    conn = db.connect(db_file)
    try:
        return [
            (row["key"], row["visibility"])
            for row in conn.execute("SELECT key, visibility FROM projects ORDER BY id")
        ]
    finally:
        conn.close()


def test_a_project_created_private_is_private_from_its_first_commit(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        _, creator, _ = bootstrap_admin_creator_outsider(client)
        created = client.post(
            "/projects",
            json={"name": "Secret", "key": "SEC", "visibility": "private"},
            headers=CREATOR,
        )
        assert created.status_code == 201, created.text
        project = created.json()
        assert project["visibility"] == "private"
        assert _projects(db_file) == [("SEC", "private")]

        # Private means private: the outsider cannot see it, the creator can, and
        # the creator is on the roster as the visibility command would put them.
        assert (
            client.get(f"/projects/{project['id']}", headers=OUTSIDER).status_code
            == 404
        )
        assert client.get(f"/projects/{project['id']}", headers=CREATOR).is_success
        roster = client.get(f"/projects/{project['id']}/members", headers=CREATOR)
        assert [row["user_id"] for row in roster.json()] == [creator["id"]]

        # Both facts are on the trail, by the creator: it was made, and narrowed.
        trail = events(db_file, "created_project", "project_made_private")
        assert sorted(
            (row["verb"], row["actor_id"], row["target_id"]) for row in trail
        ) == [
            ("created_project", creator["id"], project["id"]),
            ("project_made_private", creator["id"], project["id"]),
        ]


def test_a_public_or_omitted_visibility_records_no_flip(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap_admin_creator_outsider(client)
        for body in (
            {"name": "Open", "key": "OPN"},
            {"name": "Said", "key": "SAY", "visibility": "Public"},
        ):
            created = client.post("/projects", json=body, headers=ADMIN)
            assert created.status_code == 201, created.text
            assert created.json()["visibility"] == "public"
        assert _projects(db_file) == [("OPN", "public"), ("SAY", "public")]
        assert events(db_file, "project_made_private", "project_made_public") == []
        assert len(events(db_file, "created_project")) == 2


def test_an_omitted_visibility_keeps_the_deployment_default(tmp_path, monkeypatch):
    """Omitting the field is not asking for public: under
    ATHENA_DEFAULT_VISIBILITY=private a project is still born private, and only
    an explicit ``public`` widens it, on the trail."""
    monkeypatch.setattr(config, "DEFAULT_VISIBILITY", "private")
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap_admin_creator_outsider(client)
        for body in (
            {"name": "Quiet", "key": "QUI"},
            {"name": "Loud", "key": "LOU", "visibility": "public"},
        ):
            created = client.post("/projects", json=body, headers=CREATOR)
            assert created.status_code == 201, created.text
        assert _projects(db_file) == [("QUI", "private"), ("LOU", "public")]
        widened = events(db_file, "project_made_public", "project_made_private")
        assert [(row["verb"], row["detail"]) for row in widened] == [
            ("project_made_public", "Loud")
        ]


def test_an_unknown_visibility_is_refused_and_creates_nothing(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap_admin_creator_outsider(client)
        refused = client.post(
            "/projects",
            json={"name": "Odd", "key": "ODD", "visibility": "secret"},
            headers=ADMIN,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "validation_error"
        assert _projects(db_file) == []
        assert events(db_file, "created_project") == []
