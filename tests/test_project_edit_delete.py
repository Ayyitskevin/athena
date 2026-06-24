"""Editing and deleting a project — the API (PATCH/DELETE) and the web edit page.

These encode the contract, not just HTTP shapes:

  * a PATCH is partial — editing only the description must not blank the name;
  * a project's name stays UNIQUE on rename (a collision with ANOTHER project is
    409), but renaming to your own current name is a no-op, not a conflict;
  * edit and delete are a WRITE, and projects are creator-only (no assignee
    exists to be a second writer) — a bystander gets 403, logged-out gets 401;
  * delete REFUSES (409) while issues still belong to the project — we never
    cascade or silently detach, so the issues must be emptied first. This mirrors
    the Mentor page-delete-on-children rule: a delete must not move data by
    surprise. The project survives the refused delete.
"""
from fastapi.testclient import TestClient

from athena.aegis import projects
from athena.core import db
from athena.main import create_app


def _seed_two_users(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("ann@e.com", "Ann"))
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("bob@e.com", "Bob"))
    conn.commit()
    conn.close()


def _make_project(client, actor="1", name="Apollo", description="moon shots") -> dict:
    r = client.post(
        "/projects",
        json={"name": name, "description": description},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_issue(client, project_id, actor="1") -> int:
    r = client.post(
        "/issues",
        json={"title": "t", "project_id": project_id},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- REST API: edit -------------------------------------------------------


def test_patch_edits_name_and_description(tmp_path):
    db_file = tmp_path / "a.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        r = client.patch(
            f"/projects/{proj['id']}",
            json={"name": "Artemis", "description": "the sequel"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Artemis"
        assert r.json()["description"] == "the sequel"
        # Persists across a fresh read.
        again = client.get(f"/projects/{proj['id']}").json()
        assert again["name"] == "Artemis"
        assert again["description"] == "the sequel"


def test_patch_is_partial_description_only_leaves_name(tmp_path):
    # WHY: a partial update must not clobber fields the client didn't send.
    db_file = tmp_path / "b.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client, name="keep me", description="old")
        r = client.patch(
            f"/projects/{proj['id']}",
            json={"description": "new"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "keep me"  # untouched
        assert r.json()["description"] == "new"


def test_patch_rejects_empty_name(tmp_path):
    # WHY: name is NOT NULL/UNIQUE and a nameless project is meaningless — a blank
    # name is a 422 at the boundary, never written.
    db_file = tmp_path / "c.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        r = client.patch(
            f"/projects/{proj['id']}",
            json={"name": "   "},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_patch_empty_body_is_422(tmp_path):
    # WHY: a PATCH that sends nothing to change is a client mistake, surfaced as
    # 422 rather than a silent no-op the caller might read as success.
    db_file = tmp_path / "d.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        r = client.patch(
            f"/projects/{proj['id']}", json={}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 422


def test_patch_rename_onto_another_project_is_409(tmp_path):
    # WHY: name is UNIQUE — renaming onto a name another project already holds is
    # the same collision create guards against.
    db_file = tmp_path / "e.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        _make_project(client, name="Apollo")
        other = _make_project(client, name="Gemini")
        r = client.patch(
            f"/projects/{other['id']}",
            json={"name": "Apollo"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 409


def test_patch_rename_to_own_current_name_is_allowed(tmp_path):
    # WHY: the uniqueness guard must exclude the row being edited — re-saving your
    # own name (e.g. alongside a description tweak) is not a conflict.
    db_file = tmp_path / "f.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client, name="Apollo")
        r = client.patch(
            f"/projects/{proj['id']}",
            json={"name": "Apollo", "description": "tweaked"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["description"] == "tweaked"


def test_patch_unknown_project_is_404(tmp_path):
    db_file = tmp_path / "g.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        r = client.patch(
            "/projects/999", json={"name": "x"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 404


def test_patch_requires_authentication(tmp_path):
    db_file = tmp_path / "h.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        r = client.patch(f"/projects/{proj['id']}", json={"name": "x"})
        assert r.status_code == 401


def test_bystander_cannot_edit_project(tmp_path):
    # WHY (load-bearing): edit is a write, and projects are creator-only — there is
    # no assignee to be a second writer. A non-creator gets 403.
    db_file = tmp_path / "i.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client, actor="1")
        r = client.patch(
            f"/projects/{proj['id']}",
            json={"name": "hijacked"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 403


# --- REST API: delete -----------------------------------------------------


def test_delete_empty_project(tmp_path):
    db_file = tmp_path / "j.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        r = client.delete(
            f"/projects/{proj['id']}", headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 204
        assert client.get(f"/projects/{proj['id']}").status_code == 404


def test_delete_with_issues_is_409_and_project_survives(tmp_path):
    # WHY (load-bearing): we never cascade or silently detach. A project that still
    # owns issues is refused (409) until those issues are emptied — and it must
    # still exist after the refusal.
    db_file = tmp_path / "k.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        _make_issue(client, proj["id"], actor="1")
        r = client.delete(
            f"/projects/{proj['id']}", headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 409
        assert client.get(f"/projects/{proj['id']}").status_code == 200  # survives


def test_delete_unknown_project_is_404(tmp_path):
    db_file = tmp_path / "l.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        r = client.delete("/projects/999", headers={"X-Athena-Actor": "1"})
        assert r.status_code == 404


def test_bystander_cannot_delete_project(tmp_path):
    db_file = tmp_path / "m.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client, actor="1")
        r = client.delete(
            f"/projects/{proj['id']}", headers={"X-Athena-Actor": "2"}
        )
        assert r.status_code == 403
        assert client.get(f"/projects/{proj['id']}").status_code == 200


def test_delete_requires_authentication(tmp_path):
    db_file = tmp_path / "n.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_two_users(db_file)
        proj = _make_project(client)
        r = client.delete(f"/projects/{proj['id']}")
        assert r.status_code == 401


# --- Web edit page --------------------------------------------------------


def _login(client, email, name):
    # The first user is the bootstrap (no actor header); later creates act as the
    # session. We always create as user 1 once the first login has run.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    # Browser writes carry a CSRF token; echo the cookie in the header the
    # TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _bootstrap_first_user(client, email="ann@e.com", name="Ann"):
    # User 1: no actor header (creation is open until the first user exists).
    client.post("/users", json={"email": email, "name": name, "password": "secret"})
    client.post("/login", data={"email": email, "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_edit_form_prefills_and_is_creator_gated(tmp_path):
    # WHY: the edit form must come up prefilled (you edit, not retype), and only
    # for the creator — a logged-out user gets 401, a bystander 403.
    db_file = tmp_path / "o.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)  # user 1 — creator
        proj = _make_project(client, actor="1", name="Prefilled", description="desc here")

        form = client.get(f"/aegis/projects/{proj['id']}/edit")
        assert form.status_code == 200
        assert "Prefilled" in form.text
        assert "desc here" in form.text

        # logged out → 401
        client.cookies.clear()
        out = client.get(f"/aegis/projects/{proj['id']}/edit")
        assert out.status_code == 401

        # a different logged-in user → 403
        _login(client, "bob@e.com", "Bob")  # user 2 — bystander
        r = client.get(f"/aegis/projects/{proj['id']}/edit")
        assert r.status_code == 403


def test_web_edit_saves_and_redirects(tmp_path):
    db_file = tmp_path / "p.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)
        proj = _make_project(client, actor="1")
        r = client.post(
            f"/aegis/projects/{proj['id']}/edit",
            data={"name": "Renamed", "description": "new words"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/aegis/projects"
        again = client.get(f"/projects/{proj['id']}").json()
        assert again["name"] == "Renamed"
        assert again["description"] == "new words"


def test_web_edit_rejects_empty_name(tmp_path):
    db_file = tmp_path / "q.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)
        proj = _make_project(client, actor="1")
        r = client.post(
            f"/aegis/projects/{proj['id']}/edit",
            data={"name": "   ", "description": "x"},
        )
        assert r.status_code == 400


def test_web_delete_empty_project_redirects(tmp_path):
    db_file = tmp_path / "r.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)
        proj = _make_project(client, actor="1")
        r = client.post(
            f"/aegis/projects/{proj['id']}/delete", follow_redirects=False
        )
        assert r.status_code == 303
        assert client.get(f"/projects/{proj['id']}").status_code == 404


def test_web_delete_with_issues_is_409_and_survives(tmp_path):
    # WHY: the web delete obeys the same no-cascade rule as the API — a project
    # with issues is refused, not silently emptied.
    db_file = tmp_path / "s.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)
        proj = _make_project(client, actor="1")
        _make_issue(client, proj["id"], actor="1")
        r = client.post(f"/aegis/projects/{proj['id']}/delete")
        assert r.status_code == 409
        assert client.get(f"/projects/{proj['id']}").status_code == 200


def test_web_bystander_cannot_delete(tmp_path):
    db_file = tmp_path / "t.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)  # user 1 — creator
        proj = _make_project(client, actor="1")
        _login(client, "bob@e.com", "Bob")  # user 2 — bystander (current session)
        r = client.post(f"/aegis/projects/{proj['id']}/delete")
        assert r.status_code == 403
        assert client.get(f"/projects/{proj['id']}").status_code == 200


def test_web_list_shows_edit_link_only_to_creator(tmp_path):
    # WHY: the Edit affordance is creator-only in the UI, matching the route's 403.
    db_file = tmp_path / "u.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _bootstrap_first_user(client)  # user 1 — creator
        proj = _make_project(client, actor="1")
        edit_href = f"/aegis/projects/{proj['id']}/edit"

        creator_view = client.get("/aegis/projects").text
        assert edit_href in creator_view

        _login(client, "bob@e.com", "Bob")  # user 2 — not the creator
        bystander_view = client.get("/aegis/projects").text
        assert edit_href not in bystander_view


# --- unit: data layer precondition ----------------------------------------


def test_update_project_partial_leaves_unsent_fields(tmp_path):
    db_file = tmp_path / "v.db"
    app = create_app(db_file)
    with TestClient(app):  # runs migrations
        conn = db.connect(db_file)
        conn.execute("INSERT INTO users (email, name) VALUES ('z@e.com', 'Z')")
        conn.commit()
        proj = projects.create_project(conn, name="Orig", created_by=1, description="d")
        updated = projects.update_project(conn, proj["id"], description="d2")
        assert updated["name"] == "Orig"  # untouched
        assert updated["description"] == "d2"
        conn.close()
