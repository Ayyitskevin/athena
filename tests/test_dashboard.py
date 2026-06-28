"""The Aegis dashboard — a live, read-only overview of the work.

The dashboard owns no data: every number comes from aegis.dashboard, the one home
for the counting SQL, and the web route just lays them out. These tests pin the
aggregations that matter (active-only counts, zero-filled priorities, the backlog
row, what's in flight, the signed-in user's open plate) and that the page renders.
"""
from fastapi.testclient import TestClient

from athena.aegis import dashboard
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}  # the bootstrap admin


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"}, headers=H1)


def _login(client, email="a@e.com", name="A", password="pw"):
    client.post("/users", json={"email": email, "name": name, "password": password}, headers=H1)
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _project(client, name="Ops", key="OPS"):
    return client.post("/projects", json={"name": name, "key": key}, headers=H1).json()["id"]


def _issue(client, title="x", *, assignee_id=None, **fields):
    iid = client.post("/issues", json={"title": title, **fields}, headers=H1).json()["id"]
    if assignee_id is not None:
        r = client.put(f"/issues/{iid}/assignee", json={"assignee_id": assignee_id}, headers=H1)
        assert r.status_code == 200, r.text
    return iid


def _set_status(client, iid, status):
    r = client.patch(f"/issues/{iid}", json={"status": status}, headers=H1)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Data layer — the aggregations, asserted on a real connection to the same db.
# ---------------------------------------------------------------------------

def test_totals_count_only_active_issues(tmp_path):
    db_file = tmp_path / "totals.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _project(client)
        keep = _issue(client, "keep")
        gone = _issue(client, "gone")
        client.post(f"/issues/{gone}/archive", headers=H1)

    conn = db.connect(db_file)
    t = dashboard.totals(conn)
    # The archived issue drops out of the headline count, like it drops out of lists.
    assert t["active_issues"] == 1
    assert t["projects"] == 1
    assert t["active_sprints"] == 0
    assert dashboard.status_counts(conn) == [{"status": "open", "count": 1}]
    # Sanity: the surviving issue is the one we kept.
    assert keep != gone


def test_priority_counts_show_every_priority_in_order(tmp_path):
    db_file = tmp_path / "prio.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _issue(client, "hot", priority="high")
        _issue(client, "hot2", priority="high")

    conn = db.connect(db_file)
    counts = dashboard.priority_counts(conn)
    # Every priority shows — even at zero — in canonical low→urgent order, so the
    # shape of the backlog is always legible.
    assert [c["priority"] for c in counts] == ["low", "medium", "high", "urgent"]
    by = {c["priority"]: c["count"] for c in counts}
    assert by == {"low": 0, "medium": 0, "high": 2, "urgent": 0}


def test_priority_counts_exclude_archived(tmp_path):
    db_file = tmp_path / "prio_arch.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _issue(client, "live", priority="high")
        gone = _issue(client, "gone", priority="high")
        client.post(f"/issues/{gone}/archive", headers=H1)

    conn = db.connect(db_file)
    by = {c["priority"]: c["count"] for c in dashboard.priority_counts(conn)}
    # The archived issue drops out of the priority breakdown, like every other count.
    assert by["high"] == 1


def test_project_and_backlog_counts_split_cleanly(tmp_path):
    db_file = tmp_path / "proj.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _project(client)
        _issue(client, "in project", project_id=pid)
        _issue(client, "in project 2", project_id=pid)
        _issue(client, "loose")  # no project → backlog

    conn = db.connect(db_file)
    projs = dashboard.project_open_counts(conn)
    assert len(projs) == 1 and projs[0]["count"] == 2 and projs[0]["key"] == "OPS"
    # The projectless issue is NOT in any project row — it's the backlog.
    assert dashboard.backlog_count(conn) == 1


def test_project_with_no_issues_still_shows_at_zero(tmp_path):
    db_file = tmp_path / "empty_proj.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _project(client, "Empty", "EMP")

    conn = db.connect(db_file)
    projs = dashboard.project_open_counts(conn)
    # LEFT JOIN: a project with no issues still appears, at zero.
    assert projs == [{"id": 1, "name": "Empty", "key": "EMP", "count": 0}]


def test_archived_issue_drops_out_of_project_count(tmp_path):
    db_file = tmp_path / "arch_proj.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _project(client)
        _issue(client, "live", project_id=pid)
        gone = _issue(client, "gone", project_id=pid)
        client.post(f"/issues/{gone}/archive", headers=H1)

    conn = db.connect(db_file)
    assert dashboard.project_open_counts(conn)[0]["count"] == 1
    assert dashboard.backlog_count(conn) == 0


def test_my_open_issues_excludes_done_and_unassigned(tmp_path):
    db_file = tmp_path / "mine.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)  # user 1
        client.post("/users", json={"email": "b@e.com", "name": "B"}, headers=H1)  # user 2
        mine_open = _issue(client, "mine open", assignee_id=1)
        mine_done = _issue(client, "mine done", assignee_id=1)
        _set_status(client, mine_done, "done")
        _issue(client, "someone else's", assignee_id=2)

    conn = db.connect(db_file)
    rows = dashboard.my_open_issues(conn, 1)
    titles = [r["title"] for r in rows]
    # Only my still-open issue: the done one and the other user's are both gone.
    assert titles == ["mine open"]
    assert all(r["id"] == mine_open for r in rows)


def test_my_open_issues_caps_to_the_oldest(tmp_path):
    db_file = tmp_path / "limit.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        for n in range(5):
            _issue(client, f"task {n}", assignee_id=1)

    conn = db.connect(db_file)
    rows = dashboard.my_open_issues(conn, 1, limit=3)
    # The cap keeps the OLDEST (lowest-id) issues, in order — not the wrong end.
    assert [r["title"] for r in rows] == ["task 0", "task 1", "task 2"]


def test_active_sprints_list_in_flight_with_counts(tmp_path):
    db_file = tmp_path / "flight.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _project(client)
        # A planned sprint that we never start — must NOT show as in flight.
        client.post(f"/projects/{pid}/sprints", json={"name": "Later"}, headers=H1)
        active = client.post(f"/projects/{pid}/sprints", json={"name": "Now"}, headers=H1).json()["id"]
        client.post(f"/sprints/{active}/start", headers=H1)
        iid = _issue(client, "in sprint", project_id=pid)
        r = client.put(f"/issues/{iid}/sprint", json={"sprint_id": active}, headers=H1)
        assert r.status_code == 200, r.text

    conn = db.connect(db_file)
    flight = dashboard.active_sprints(conn)
    assert len(flight) == 1
    assert flight[0]["name"] == "Now"
    assert flight[0]["project_name"] == "Ops"
    assert flight[0]["issue_count"] == 1


def test_active_sprint_count_excludes_archived_issues(tmp_path):
    db_file = tmp_path / "flight_arch.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pid = _project(client)
        sid = client.post(f"/projects/{pid}/sprints", json={"name": "Now"}, headers=H1).json()["id"]
        client.post(f"/sprints/{sid}/start", headers=H1)
        live = _issue(client, "live", project_id=pid)
        gone = _issue(client, "gone", project_id=pid)
        for iid in (live, gone):
            assert client.put(f"/issues/{iid}/sprint", json={"sprint_id": sid}, headers=H1).status_code == 200
        # Archiving an issue keeps its sprint_id, so a naive COUNT(*) would still
        # see it — the dashboard's active-only count must not.
        client.post(f"/issues/{gone}/archive", headers=H1)

    conn = db.connect(db_file)
    flight = dashboard.active_sprints(conn)
    assert flight[0]["issue_count"] == 1


# ---------------------------------------------------------------------------
# Web route — the page renders the numbers and respects who's looking.
# ---------------------------------------------------------------------------

def test_dashboard_renders_for_anonymous(tmp_path):
    with TestClient(create_app(tmp_path / "anon.db")) as client:
        _bootstrap(client)
        _project(client)
        _issue(client, "Visible task")

        page = client.get("/aegis/dashboard")
        assert page.status_code == 200
        text = page.text
        # The standard sections are all present...
        assert "Dashboard" in text
        assert "By status" in text and "By priority" in text and "By project" in text
        assert "In flight" in text and "Recent activity" in text
        # ...but the personal plate is hidden when nobody is signed in.
        assert "My open issues" not in text


def test_dashboard_shows_my_plate_when_signed_in(tmp_path):
    with TestClient(create_app(tmp_path / "session.db")) as client:
        _login(client)  # first user → admin, now a real web session
        mine = _issue(client, "Wire the dashboard", assignee_id=1)
        assert mine

        text = client.get("/aegis/dashboard").text
        assert "My open issues" in text
        assert "Wire the dashboard" in text


def test_dashboard_backlog_row_links_to_projectless_issues(tmp_path):
    with TestClient(create_app(tmp_path / "backlog.db")) as client:
        _bootstrap(client)
        _issue(client, "loose end")

        text = client.get("/aegis/dashboard").text
        # The backlog row is always rendered, linking into the projectless view.
        assert "Backlog (no project)" in text
        assert "/aegis/issues?project=none" in text
