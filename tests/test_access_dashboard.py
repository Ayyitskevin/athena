"""The dashboard counts only what the viewer may see (access control, dashboard).

Every headline number, distribution, per-project count, and in-flight sprint on the
dashboard must exclude issues/projects/sprints in projects the viewer can't see, while
the backlog (no project) always counts and admins see everything. These exercise the
aggregations directly with a viewer's visibility set, plus one web check that the
anonymous dashboard never names a private project.
"""

from fastapi.testclient import TestClient

from athena.aegis import dashboard
from athena.core import access, db, users
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post(
        "/users",
        json={"email": "admin@e.com", "name": "Admin", "password": "pw"},
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "c@e.com",
            "name": "Creator",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "o@e.com",
            "name": "Outsider",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )


def _issue(client, title, *, project_id=None, priority="medium", headers=H_CREATOR):
    body = {"title": title, "priority": priority}
    if project_id is not None:
        body["project_id"] = project_id
    return client.post("/issues", json=body, headers=headers).json()["id"]


def _active_sprint(client, pid):
    sid = client.post(
        f"/projects/{pid}/sprints", json={"name": "S"}, headers=H_CREATOR
    ).json()["id"]
    client.post(f"/sprints/{sid}/start", headers=H_CREATOR)
    return sid


def _scenario(client, db_file):
    """Private project SEC (2 issues + active sprint), public project OPN (1 issue +
    active sprint), one backlog issue. Returns (db_conn, priv_pid, pub_pid)."""
    priv = client.post(
        "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
    ).json()["id"]
    pub = client.post(
        "/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR
    ).json()["id"]
    _issue(client, "Hidden A", project_id=priv, priority="high")
    _issue(client, "Hidden B", project_id=priv, priority="high")
    _issue(client, "Public one", project_id=pub, priority="low")
    _issue(client, "Loose one")  # backlog
    _active_sprint(client, priv)
    _active_sprint(client, pub)

    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (priv,))
    conn.commit()
    return conn, priv, pub


def _vis(conn, user_id):
    return access.visible_project_filter(conn, users.get_user(conn, user_id))


def test_totals_count_only_visible(tmp_path):
    db_file = tmp_path / "tot.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, pub = _scenario(client, db_file)

        # Outsider: public issue + backlog = 2; one project; one active sprint.
        out = dashboard.totals(conn, _vis(conn, 3))
        assert out == {"active_issues": 2, "projects": 1, "active_sprints": 1}
        # Admin (visible filter is None): everything — 4 issues, 2 projects, 2 sprints.
        admin = dashboard.totals(conn, _vis(conn, 1))
        assert admin == {"active_issues": 4, "projects": 2, "active_sprints": 2}
        # Creator sees their private project too, so the same totals as admin here.
        assert dashboard.totals(conn, _vis(conn, 2)) == admin


def test_status_and_priority_counts_exclude_hidden(tmp_path):
    db_file = tmp_path / "dist.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, pub = _scenario(client, db_file)

        # Outsider sees only the public + backlog issues, both 'open' → count 2.
        out_status = dashboard.status_counts(conn, _vis(conn, 3))
        assert out_status == [{"status": "open", "count": 2}]
        # Admin sees all four open issues.
        assert dashboard.status_counts(conn, _vis(conn, 1)) == [
            {"status": "open", "count": 4}
        ]

        # Priority: the two hidden 'high' issues vanish for the outsider.
        out_prio = {
            p["priority"]: p["count"]
            for p in dashboard.priority_counts(conn, _vis(conn, 3))
        }
        assert out_prio == {"low": 1, "medium": 1, "high": 0, "urgent": 0}
        admin_prio = {
            p["priority"]: p["count"]
            for p in dashboard.priority_counts(conn, _vis(conn, 1))
        }
        assert admin_prio == {"low": 1, "medium": 1, "high": 2, "urgent": 0}


def test_project_and_sprint_views_exclude_hidden(tmp_path):
    db_file = tmp_path / "proj.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, pub = _scenario(client, db_file)

        out_projects = dashboard.project_open_counts(conn, _vis(conn, 3))
        assert [p["key"] for p in out_projects] == ["OPN"]
        assert {
            p["key"] for p in dashboard.project_open_counts(conn, _vis(conn, 1))
        } == {"SEC", "OPN"}

        out_flight = dashboard.active_sprints(conn, _vis(conn, 3))
        assert [s["project_name"] for s in out_flight] == ["Open"]
        assert len(dashboard.active_sprints(conn, _vis(conn, 1))) == 2


def test_my_open_issues_drops_assignments_in_hidden_projects(tmp_path):
    db_file = tmp_path / "mine.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn, priv, pub = _scenario(client, db_file)
        # Assign one hidden issue and the public one to the outsider.
        hidden = client.get("/issues", headers=H_ADMIN).json()
        priv_iid = next(i["id"] for i in hidden if i["title"] == "Hidden A")
        pub_iid = next(i["id"] for i in hidden if i["title"] == "Public one")
        client.put(
            f"/issues/{priv_iid}/assignee", json={"assignee_id": 3}, headers=H_CREATOR
        )
        client.put(
            f"/issues/{pub_iid}/assignee", json={"assignee_id": 3}, headers=H_CREATOR
        )

        mine = dashboard.my_open_issues(conn, 3, visible_project_ids=_vis(conn, 3))
        # Only the public assignment shows; the hidden-project one is gone.
        assert [i["title"] for i in mine] == ["Public one"]


def test_web_dashboard_hides_private_project_from_anonymous(tmp_path):
    db_file = tmp_path / "web.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        _scenario(client, db_file)

        text = client.get("/aegis/dashboard").text
        # The private project never appears in the "By project" section; the public
        # one does.
        assert "Secret" not in text
        assert "Open" in text
