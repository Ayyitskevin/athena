"""Per-project, configurable issue statuses.

These encode the contract: every project is seeded with the default lifecycle;
statuses are validated PER PROJECT; create defaults to the project's first status;
moving an issue to a project that lacks its status remaps it; "done" is now
category-based; statuses can be added/removed (creator-only, not when in use); and
the web surfaces (board columns, project edit, detail dropdown) reflect all of it.
"""
from athena.aegis import dependencies, issues, projects, statuses
from athena.core import db
from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def _user2(client):
    client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)


def _project(client, key="ATH", name="Athena"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()


# --- data layer -------------------------------------------------------------


def test_seed_validate_and_done_category(tmp_path):
    conn = db.connect(tmp_path / "d.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    proj = projects.create_project(conn, name="P", key="P", created_by=1)
    # Seeded defaults.
    assert statuses.status_names(conn, proj["id"]) == ["open", "in_progress", "done"]
    # Backlog uses the default set too.
    assert statuses.status_names(conn, None) == ["open", "in_progress", "done"]
    assert statuses.is_valid(conn, proj["id"], "open")
    assert not statuses.is_valid(conn, proj["id"], "nope")
    assert statuses.is_done(conn, proj["id"], "done")
    assert not statuses.is_done(conn, proj["id"], "open")
    assert statuses.first_status(conn, proj["id"]) == "open"


def test_open_blockers_uses_done_category(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    proj = projects.create_project(conn, name="P", key="P", created_by=1)
    statuses.add_status(conn, proj["id"], "shipped", "done")  # a custom CLOSED status
    blocker = issues.create_issue(conn, title="b", body="", created_by=1, project_id=proj["id"])
    blocked = issues.create_issue(conn, title="a", body="", created_by=1, project_id=proj["id"])
    dependencies.add_link(
        conn, from_id=blocker["id"], to_id=blocked["id"], relation="blocks", created_by=1
    )
    # 'open' blocker is open.
    assert len(dependencies.open_blockers(conn, blocked["id"])) == 1
    # A done-CATEGORY status (not literally "done") resolves it.
    issues.update_status(conn, blocker["id"], "shipped")
    assert dependencies.open_blockers(conn, blocked["id"]) == []


# --- API: statuses + validation ---------------------------------------------


def test_new_project_has_default_statuses(tmp_path):
    with TestClient(create_app(tmp_path / "n.db")) as client:
        _admin(client)
        proj = _project(client)
        st = client.get(f"/projects/{proj['id']}/statuses").json()
        assert [s["name"] for s in st] == ["open", "in_progress", "done"]
        assert [s["category"] for s in st] == ["todo", "doing", "done"]


def test_add_custom_status_and_use_it(tmp_path):
    with TestClient(create_app(tmp_path / "a.db")) as client:
        _admin(client)
        proj = _project(client)
        r = client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        assert r.status_code == 201 and "review" in [s["name"] for s in r.json()]
        # An issue can use the new status; an unknown one is rejected.
        ok = client.post(
            "/issues",
            json={"title": "x", "project_id": proj["id"], "status": "review"},
            headers=H1,
        )
        assert ok.status_code == 201 and ok.json()["status"] == "review"
        bad = client.post(
            "/issues",
            json={"title": "y", "project_id": proj["id"], "status": "nope"},
            headers=H1,
        )
        assert bad.status_code == 422


def test_add_status_rejects_duplicate_and_bad_category(tmp_path):
    with TestClient(create_app(tmp_path / "v.db")) as client:
        _admin(client)
        proj = _project(client)
        assert (
            client.post(
                f"/projects/{proj['id']}/statuses",
                json={"name": "open", "category": "todo"},
                headers=H1,
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/projects/{proj['id']}/statuses",
                json={"name": "weird", "category": "nonsense"},
                headers=H1,
            ).status_code
            == 422
        )


def test_remove_status_rules(tmp_path):
    with TestClient(create_app(tmp_path / "r.db")) as client:
        _admin(client)
        proj = _project(client)
        client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        iss = client.post(
            "/issues",
            json={"title": "x", "project_id": proj["id"], "status": "review"},
            headers=H1,
        ).json()
        # In use -> refused.
        assert client.delete(f"/projects/{proj['id']}/statuses/review", headers=H1).status_code == 409
        # Free it, then it removes.
        client.patch(f"/issues/{iss['id']}", json={"status": "open"}, headers=H1)
        assert client.delete(f"/projects/{proj['id']}/statuses/review", headers=H1).status_code == 200


def test_status_management_is_creator_only(tmp_path):
    with TestClient(create_app(tmp_path / "c.db")) as client:
        _admin(client)
        _user2(client)
        proj = _project(client)  # created by user1
        assert (
            client.post(
                f"/projects/{proj['id']}/statuses",
                json={"name": "x", "category": "todo"},
                headers=H2,
            ).status_code
            == 403
        )


def test_create_defaults_to_first_status(tmp_path):
    with TestClient(create_app(tmp_path / "f.db")) as client:
        _admin(client)
        proj = _project(client)
        assert client.post(
            "/issues", json={"title": "x", "project_id": proj["id"]}, headers=H1
        ).json()["status"] == "open"
        assert client.post("/issues", json={"title": "y"}, headers=H1).json()["status"] == "open"


def test_update_status_validated_against_project(tmp_path):
    with TestClient(create_app(tmp_path / "u.db")) as client:
        _admin(client)
        proj = _project(client)
        client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        iss = client.post(
            "/issues", json={"title": "x", "project_id": proj["id"]}, headers=H1
        ).json()
        assert client.patch(
            f"/issues/{iss['id']}", json={"status": "review"}, headers=H1
        ).json()["status"] == "review"
        assert client.patch(
            f"/issues/{iss['id']}", json={"status": "bogus"}, headers=H1
        ).status_code == 422


def test_move_remaps_invalid_status(tmp_path):
    with TestClient(create_app(tmp_path / "m.db")) as client:
        _admin(client)
        a = _project(client, "AAA", "Alpha")
        b = _project(client, "BBB", "Beta")
        client.post(
            f"/projects/{a['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        iss = client.post(
            "/issues",
            json={"title": "x", "project_id": a["id"], "status": "review"},
            headers=H1,
        ).json()
        # B has no 'review' -> remapped to B's first status.
        moved = client.put(
            f"/issues/{iss['id']}/project", json={"project_id": b["id"]}, headers=H1
        ).json()
        assert moved["status"] == "open"


# --- web --------------------------------------------------------------------


def _login(client):
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


def test_web_board_has_dynamic_column(tmp_path):
    with TestClient(create_app(tmp_path / "wb.db")) as client:
        _admin(client)
        proj = _project(client)
        client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        client.post(
            "/issues",
            json={"title": "x", "project_id": proj["id"], "status": "review"},
            headers=H1,
        )
        assert "Review" in client.get("/aegis/boards").text  # custom column header


def test_web_project_edit_status_management(tmp_path):
    with TestClient(create_app(tmp_path / "we.db")) as client:
        _admin(client)
        csrf = _login(client)
        proj = _project(client)
        page = client.get(f"/aegis/projects/{proj['id']}/edit").text
        assert "Statuses" in page and "Open" in page
        client.post(
            f"/aegis/projects/{proj['id']}/statuses",
            data={"name": "review", "category": "doing"},
            headers={"X-CSRF-Token": csrf},
        )
        assert "Review" in client.get(f"/aegis/projects/{proj['id']}/edit").text


def test_web_issue_detail_status_dropdown_uses_project_statuses(tmp_path):
    with TestClient(create_app(tmp_path / "wd.db")) as client:
        _admin(client)
        _login(client)
        proj = _project(client)
        client.post(
            f"/projects/{proj['id']}/statuses",
            json={"name": "review", "category": "doing"},
            headers=H1,
        )
        iss = client.post(
            "/issues", json={"title": "x", "project_id": proj["id"]}, headers=H1
        ).json()
        page = client.get(f"/aegis/issues/{iss['id']}").text
        assert 'value="review"' in page
