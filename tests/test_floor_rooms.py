"""Rooms group the floor. They are not a second tracker."""

from fastapi.testclient import TestClient

from athena.aegis import issues, office, room_commands, rooms
from athena.core import db, users
from athena.main import create_app


def test_starter_pack_and_place_issue(tmp_path):
    conn = db.connect(tmp_path / "rooms.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    from athena.aegis import projects

    project = projects.create_project(
        conn, name="Scranton", key="SCR", created_by=admin["id"]
    )
    created = room_commands.stock_starter_rooms(
        conn, actor=admin, project_id=project["id"]
    )
    assert {row["slug"] for row in created} == {
        "warehouse",
        "accounting",
        "sales",
        "annex",
    }
    again = room_commands.stock_starter_rooms(
        conn, actor=admin, project_id=project["id"]
    )
    assert again == []
    issue = issues.create_issue(
        conn,
        title="forklift",
        body="",
        created_by=admin["id"],
        project_id=project["id"],
    )
    warehouse = rooms.get_room_by_slug(conn, project["id"], "warehouse")
    assert warehouse is not None
    room_commands.place_issue(
        conn, actor=admin, issue_id=issue["id"], room_id=warehouse["id"]
    )
    floor = office.build_floor(conn, project_id=project["id"], actor=admin)
    assert floor is not None
    warehouse_section = next(s for s in floor["sections"] if s["slug"] == "warehouse")
    assert warehouse_section["chairs"][0]["issue_title"] == "forklift"
    filtered = office.build_floor(
        conn, project_id=project["id"], actor=admin, room_slug="warehouse"
    )
    assert filtered is not None
    assert all(
        chair["room"] and chair["room"]["slug"] == "warehouse"
        for chair in filtered["chairs"]
    )
    conn.close()


def test_room_commands_refuse_bad_place_and_blank_name(tmp_path):
    from athena.aegis import projects

    conn = db.connect(tmp_path / "rooms-refuse.db")
    db.migrate(conn)
    admin = users.create_user(
        conn, email="admin@e.com", name="Admin", role=users.ADMIN_ROLE
    )
    project = projects.create_project(
        conn, name="Scranton", key="SCR", created_by=admin["id"]
    )
    other = projects.create_project(
        conn, name="Stamford", key="STF", created_by=admin["id"]
    )
    try:
        room_commands.create_room(
            conn, actor=admin, project_id=project["id"], name="   "
        )
        raise AssertionError("blank")
    except room_commands.RoomCommandError as exc:
        assert exc.kind == "invalid"
    warehouse = room_commands.create_room(
        conn, actor=admin, project_id=project["id"], name="The Warehouse"
    )
    try:
        room_commands.create_room(
            conn, actor=admin, project_id=project["id"], name="The Warehouse"
        )
        raise AssertionError("dup")
    except room_commands.RoomCommandError as exc:
        assert exc.kind == "conflict"
    backlog = issues.create_issue(conn, title="orphan", body="", created_by=admin["id"])
    try:
        room_commands.place_issue(
            conn, actor=admin, issue_id=backlog["id"], room_id=warehouse["id"]
        )
        raise AssertionError("backlog")
    except room_commands.RoomCommandError as exc:
        assert "backlog" in exc.detail
    foreign = issues.create_issue(
        conn, title="other", body="", created_by=admin["id"], project_id=other["id"]
    )
    try:
        room_commands.place_issue(
            conn, actor=admin, issue_id=foreign["id"], room_id=warehouse["id"]
        )
        raise AssertionError("foreign")
    except room_commands.RoomCommandError as exc:
        assert exc.kind == "invalid"
    annex = office.build_floor(
        conn, project_id=project["id"], actor=admin, room_slug="unassigned"
    )
    assert annex is not None
    assert annex["filter"] == "unassigned"
    conn.close()


def test_floor_html_stocks_and_filters(tmp_path):
    app = create_app(tmp_path / "rooms-web.db")
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin", "password": "secret"},
        )
        project = client.post(
            "/projects",
            json={"key": "SCR", "name": "Scranton"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        client.post(
            "/issues",
            json={"title": "empty chair", "project_id": project["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        client.post(
            "/login",
            data={"email": "admin@e.com", "password": "secret"},
            follow_redirects=False,
        )
        client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
        stock = client.post(
            f"/aegis/projects/{project['id']}/rooms/stock",
            follow_redirects=False,
        )
        assert stock.status_code == 303, stock.text
        page = client.get(f"/aegis/projects/{project['id']}/floor")
        assert page.status_code == 200
        assert "The Warehouse" in page.text
        assert "The Annex" in page.text
        payload = client.get(
            f"/projects/{project['id']}/floor", params={"room": "warehouse"}
        ).json()
        assert payload["filter"] == "warehouse"
