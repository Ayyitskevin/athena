"""Tests for projects — a named grouping an issue may belong to (or not).

A project is created by any signed-in actor and groups related issues via a
single nullable column (issues.project_id). These tests encode the contract:

  * projects are created by any signed-in actor; names are case-insensitively
    unique, and re-creating one is a 409 not a duplicate;
  * an issue may be created into a project, and moving it between projects (or
    out of one) is a WRITE — so it obeys the same creator-or-assignee gate as
    status/priority/assignment (bystander -> 403);
  * an unknown project is rejected at the boundary (422), never an FK 500;
  * every issue read carries its project_id + project_name (NULL when none), and
    the issue list can be filtered to one project.
"""

import re

from fastapi.testclient import TestClient

from athena.aegis import issues, projects
from athena.core import db
from athena.main import create_app


def _migrated_conn(db_file):
    """A connection to a freshly-migrated DB, for unit tests that touch the
    data-access layer directly (create_app only migrates inside its lifespan)."""
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_three_users(db_file):
    # creator=1, assignee=2, bystander=3 — the standard authz cast.
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('ann@e.com', 'Ann')")
    conn.execute("INSERT INTO users (email, name) VALUES ('bob@e.com', 'Bob')")
    conn.execute("INSERT INTO users (email, name) VALUES ('cy@e.com', 'Cy')")
    conn.commit()
    conn.close()


def _make_issue(client, actor="1", **body) -> dict:
    r = client.post(
        "/issues", json={"title": "t", **body}, headers={"X-Athena-Actor": actor}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _key_for(name: str) -> str:
    """A valid key derived from a project name, so test helpers don't have to spell
    one out every time. Strip to letters/digits, uppercase, cap at 10."""
    return re.sub(r"[^A-Za-z0-9]", "", name).upper()[:10]


def _create_project(client, name, actor="1", description="", key=None) -> dict:
    r = client.post(
        "/projects",
        json={"name": name, "key": key or _key_for(name), "description": description},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- unit: data access ----------------------------------------------------


def test_create_and_get_project_round_trip(tmp_path):
    conn = _migrated_conn(tmp_path / "p.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    made = projects.create_project(
        conn, name="Website", key="WEB", description="the redesign", created_by=1
    )
    got = projects.get_project(conn, made["id"])
    assert got["name"] == "Website"
    assert got["description"] == "the redesign"
    assert got["created_by"] == 1


def test_get_project_by_name_is_case_insensitive(tmp_path):
    conn = _migrated_conn(tmp_path / "pn.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    projects.create_project(conn, name="Mobile", key="MOB", created_by=1)
    assert projects.get_project_by_name(conn, "mobile")["name"] == "Mobile"


def test_list_projects_is_alphabetical(tmp_path):
    conn = _migrated_conn(tmp_path / "pl.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    projects.create_project(conn, name="Zebra", key="ZEB", created_by=1)
    projects.create_project(conn, name="apple", key="APP", created_by=1)
    assert [p["name"] for p in projects.list_projects(conn)] == ["apple", "Zebra"]


def test_set_project_sets_then_clears(tmp_path):
    # WHY: project is a single nullable column — set_project must both move an
    # issue into a project AND remove it (None), distinct operations on one field.
    conn = _migrated_conn(tmp_path / "sp.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    proj = projects.create_project(conn, name="Core", key="CORE", created_by=1)
    issue = issues.create_issue(conn, title="t", body="", created_by=1)
    moved = issues.set_project(conn, issue["id"], proj["id"])
    assert moved["project_id"] == proj["id"]
    assert moved["project_name"] == "Core"  # composed in via the LEFT JOIN
    cleared = issues.set_project(conn, issue["id"], None)
    assert cleared["project_id"] is None
    assert cleared["project_name"] is None


# --- API: the project resource --------------------------------------------


def test_create_then_list_and_get_project(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "api.db")
        made = _create_project(client, "Atlas", description="big one")
        assert made["name"] == "Atlas"
        assert made["created_by"] == 1  # stamped from the actor

        assert [p["name"] for p in client.get("/projects").json()] == ["Atlas"]

        got = client.get(f"/projects/{made['id']}")
        assert got.status_code == 200
        assert got.json()["description"] == "big one"


def test_project_list_is_open_to_anyone(tmp_path):
    # WHY: reads are open — listing projects needs no actor, like listing issues.
    app = create_app(tmp_path / "open.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "open.db")
        _create_project(client, "Public")
        assert client.get("/projects").status_code == 200  # no actor header


def test_create_project_requires_actor(tmp_path):
    app = create_app(tmp_path / "noactor.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "noactor.db")
        r = client.post("/projects", json={"name": "Anon", "key": "AN"})
        assert r.status_code == 401


def test_empty_project_name_is_rejected(tmp_path):
    app = create_app(tmp_path / "empty.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "empty.db")
        r = client.post(
            "/projects",
            json={"name": "   ", "key": "EMPTY"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_duplicate_project_name_is_409(tmp_path):
    # WHY: names are case-insensitively unique — a second "Atlas" is a conflict,
    # not a silent duplicate.
    app = create_app(tmp_path / "dup.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "dup.db")
        _create_project(client, "Atlas")
        r = client.post(
            "/projects",
            json={"name": "atlas", "key": "ATL2"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 409


def test_get_missing_project_is_404(tmp_path):
    app = create_app(tmp_path / "miss.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "miss.db")
        assert client.get("/projects/999").status_code == 404


# --- API: an issue's membership in a project ------------------------------


def test_create_issue_into_a_project(tmp_path):
    app = create_app(tmp_path / "into.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "into.db")
        proj = _create_project(client, "Core")
        issue = _make_issue(client, title="in core", project_id=proj["id"])
        assert issue["project_id"] == proj["id"]
        assert issue["project_name"] == "Core"  # name composed onto the read


def test_create_issue_with_unknown_project_is_422(tmp_path):
    # WHY: an unknown project is caught at the boundary, never an FK 500.
    app = create_app(tmp_path / "badproj.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "badproj.db")
        r = client.post(
            "/issues",
            json={"title": "t", "project_id": 999},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_issue_without_project_reads_null(tmp_path):
    app = create_app(tmp_path / "noproj.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "noproj.db")
        issue = _make_issue(client, title="loose")
        assert issue["project_id"] is None
        assert issue["project_name"] is None


def test_set_project_moves_and_removes_issue(tmp_path):
    app = create_app(tmp_path / "move.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "move.db")
        proj = _create_project(client, "Core")
        issue = _make_issue(client, title="t")
        r = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": proj["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["project_name"] == "Core"
        # Remove it again (None = no project).
        r = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": None},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["project_id"] is None


def test_set_unknown_project_is_422(tmp_path):
    app = create_app(tmp_path / "setbad.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "setbad.db")
        issue = _make_issue(client, title="t")
        r = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": 999},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_set_project_on_missing_issue_is_404(tmp_path):
    app = create_app(tmp_path / "setmiss.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "setmiss.db")
        proj = _create_project(client, "Core")
        r = client.put(
            "/issues/999/project",
            json={"project_id": proj["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 404


def test_bystander_cannot_set_project(tmp_path):
    # WHY: moving an issue between projects is a WRITE — only the creator or
    # current assignee may do it; every other actor is a 403.
    app = create_app(tmp_path / "by.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "by.db")
        proj = _create_project(client, "Core")
        issue = _make_issue(client, actor="1", title="t")  # created by user 1
        r = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": proj["id"]},
            headers={"X-Athena-Actor": "3"},  # bystander
        )
        assert r.status_code == 403


def test_assignee_can_set_project(tmp_path):
    # WHY: the gate is creator OR current assignee — once user 2 is assigned,
    # they may move the issue between projects.
    app = create_app(tmp_path / "asg.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "asg.db")
        proj = _create_project(client, "Core")
        issue = _make_issue(client, actor="1", title="t")
        assign = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": 2},
            headers={"X-Athena-Actor": "1"},
        )
        assert assign.status_code == 200
        r = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": proj["id"]},
            headers={"X-Athena-Actor": "2"},  # the assignee
        )
        assert r.status_code == 200


# --- API: filtering the issue list by project -----------------------------


def test_list_filters_by_project(tmp_path):
    # WHY: ?project=<id> narrows the list to one project, through the same shared
    # list_issues path the web list uses.
    app = create_app(tmp_path / "filt.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "filt.db")
        core = _create_project(client, "Core")
        _create_project(client, "Side")
        _make_issue(client, title="in core", project_id=core["id"])
        _make_issue(client, title="no project")
        titles = [
            i["title"] for i in client.get(f"/issues?project={core['id']}").json()
        ]
        assert titles == ["in core"]


def test_project_filter_composes_with_status(tmp_path):
    # WHY: project + status must AND together — narrowing, not replacing.
    app = create_app(tmp_path / "compose.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "compose.db")
        core = _create_project(client, "Core")
        _make_issue(client, title="done in core", status="done", project_id=core["id"])
        _make_issue(client, title="open in core", status="open", project_id=core["id"])
        titles = [
            i["title"]
            for i in client.get(f"/issues?project={core['id']}&status=done").json()
        ]
        assert titles == ["done in core"]


# --- web layer ------------------------------------------------------------


def _login(client, email, name):
    # User creation is authenticated after the first (bootstrap) user; act as
    # user 1, who always exists once the first _login has run.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_create_project(tmp_path):
    # WHY: a project is deliberately created (not find-or-create like labels), so
    # the web has a real create form; the actor is the session user, not a field.
    app = create_app(tmp_path / "wc.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1
        r = client.post(
            "/aegis/projects",
            data={"name": "Website", "key": "WEB", "description": "redesign"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        made = client.get("/projects").json()
        assert [p["name"] for p in made] == ["Website"]
        assert made[0]["created_by"] == 1
        # and it renders on the projects page
        assert "Website" in client.get("/aegis/projects").text


def test_web_create_project_requires_login(tmp_path):
    app = create_app(tmp_path / "wl.db")
    with TestClient(app) as client:
        r = client.post("/aegis/projects", data={"name": "Anon"})
        assert r.status_code == 401


def test_web_empty_project_name_is_400(tmp_path):
    app = create_app(tmp_path / "we.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        r = client.post("/aegis/projects", data={"name": "   "})
        assert r.status_code == 400


def test_web_duplicate_project_name_is_409(tmp_path):
    app = create_app(tmp_path / "wd.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        client.post("/aegis/projects", data={"name": "Atlas", "key": "ATL"})
        r = client.post("/aegis/projects", data={"name": "atlas", "key": "ATL2"})
        assert r.status_code == 409


def test_web_set_project_on_issue(tmp_path):
    app = create_app(tmp_path / "ws.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 = creator
        client.post("/aegis/projects", data={"name": "Core", "key": "CORE"})
        proj = client.get("/projects").json()[0]
        issue = _make_issue(client, actor="1", title="t")
        r = client.post(
            f"/aegis/issues/{issue['id']}/project",
            data={"project_id": str(proj["id"])},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get(f"/aegis/issues/{issue['id']}").text
        assert "Core" in page  # project name renders in the sidebar


def test_web_bystander_cannot_set_project(tmp_path):
    # WHY: setting a project is a write — the web path enforces the same
    # creator-or-assignee gate as the API (bystander -> 403).
    app = create_app(tmp_path / "wb.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator
        client.post("/aegis/projects", data={"name": "Core", "key": "CORE"})
        proj = client.get("/projects").json()[0]
        issue = _make_issue(client, actor="1", title="t")
        _login(client, "bob@e.com", "Bob")  # user 2 now the session — bystander
        r = client.post(
            f"/aegis/issues/{issue['id']}/project",
            data={"project_id": str(proj["id"])},
        )
        assert r.status_code == 403


def test_web_list_filters_by_project(tmp_path):
    # WHY: the ?project= filter on the web list goes through the same shared
    # list_issues path as the API — match shows, non-match hidden.
    app = create_app(tmp_path / "wf.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        client.post("/aegis/projects", data={"name": "Core", "key": "CORE"})
        proj = client.get("/projects").json()[0]
        _make_issue(client, actor="1", title="in core", project_id=proj["id"])
        _make_issue(client, actor="1", title="loose one")
        page = client.get(f"/aegis/issues?project={proj['id']}").text
        assert "in core" in page
        assert "loose one" not in page


# --- backlog filter (issues with no project) ------------------------------


def test_list_issues_backlog_only_unprojected(tmp_path):
    # WHY: backlog=True is a DISTINCT filter from project_id=None — it restricts
    # to issues with no project, where project_id=None means "don't filter".
    conn = _migrated_conn(tmp_path / "bl.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    proj = projects.create_project(conn, name="Core", key="CORE", created_by=1)
    issues.create_issue(
        conn, title="in core", body="", created_by=1, project_id=proj["id"]
    )
    issues.create_issue(conn, title="loose one", body="", created_by=1)
    backlog = [i["title"] for i in issues.list_issues(conn, backlog=True)]
    assert backlog == ["loose one"]
    # project_id=None (the default) does NOT filter — both issues come back.
    assert len(issues.list_issues(conn)) == 2


def test_api_filter_backlog_with_project_none(tmp_path):
    # WHY: ?project=none narrows the list to backlog issues through the same
    # shared list_issues path; an id still selects that project, garbage is 422.
    app = create_app(tmp_path / "apibl.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "apibl.db")
        core = _create_project(client, "Core")
        _make_issue(client, title="in core", project_id=core["id"])
        _make_issue(client, title="no project")
        backlog = [i["title"] for i in client.get("/issues?project=none").json()]
        assert backlog == ["no project"]
        # the id form is unaffected by the sentinel
        assert [
            i["title"] for i in client.get(f"/issues?project={core['id']}").json()
        ] == ["in core"]
        # a non-"none", non-numeric value stays a 422 (param is still strict)
        assert client.get("/issues?project=bogus").status_code == 422


def test_api_backlog_composes_with_status(tmp_path):
    # WHY: backlog must AND with other filters, not replace them.
    app = create_app(tmp_path / "blcompose.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "blcompose.db")
        _make_issue(client, title="done loose", status="done")
        _make_issue(client, title="open loose", status="open")
        titles = [
            i["title"] for i in client.get("/issues?project=none&status=done").json()
        ]
        assert titles == ["done loose"]


def test_web_list_filters_to_backlog(tmp_path):
    # WHY: the dropdown's "No project (backlog)" option posts project=none and the
    # web list shows only unprojected issues — same shared path as the API.
    app = create_app(tmp_path / "wbl.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        client.post("/aegis/projects", data={"name": "Core", "key": "CORE"})
        proj = client.get("/projects").json()[0]
        _make_issue(client, actor="1", title="in core", project_id=proj["id"])
        _make_issue(client, actor="1", title="loose one")
        page = client.get("/aegis/issues?project=none").text
        assert "loose one" in page
        assert "in core" not in page


def test_web_list_offers_backlog_option(tmp_path):
    # WHY: the backlog filter is only reachable if the dropdown renders the option.
    app = create_app(tmp_path / "wopt.db")
    with TestClient(app) as client:
        _seed_three_users(tmp_path / "wopt.db")
        page = client.get("/aegis/issues").text
        assert 'value="none"' in page
        assert "No project (backlog)" in page
