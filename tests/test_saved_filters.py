"""Saved filters — named, reusable issue queries owned by a user.

These encode the contract: a filter stores a normalized set of criteria; running
it returns exactly the issues matching ALL of them through the SAME path the issue
list uses; filters are private (owner-scoped, a stranger's filter is a 404);
duplicate names per owner are rejected; closed-set criteria (priority/project) are
validated while open ones (status/label/assignee) stay resilient; and the web and
API surfaces agree because they share normalize/validate/run.
"""

from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})


def _user2(client):
    client.post("/users", json={"email": "b@e.com", "name": "Bob"}, headers=H1)


def _issue(client, title, **kw):
    return client.post("/issues", json={"title": title, **kw}, headers=H1).json()


# --- API: CRUD round trip ---------------------------------------------------


def test_create_list_get_filter(tmp_path):
    """A created filter comes back on the owner's list and by id, with its criteria
    normalized to the canonical stored form (empties dropped, types coerced)."""
    with TestClient(create_app(tmp_path / "f.db")) as client:
        _admin(client)
        r = client.post(
            "/filters",
            json={
                "name": "My bugs",
                "criteria": {"priority": "high", "search": " login "},
            },
            headers=H1,
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "My bugs"
        # Whitespace stripped, only set keys kept.
        assert body["criteria"] == {"priority": "high", "search": "login"}
        fid = body["id"]
        assert [f["name"] for f in client.get("/filters", headers=H1).json()] == [
            "My bugs"
        ]
        assert client.get(f"/filters/{fid}", headers=H1).json()["id"] == fid


def test_empty_criteria_is_allowed(tmp_path):
    """A filter with no criteria is legal — it's a saved 'all issues' view."""
    with TestClient(create_app(tmp_path / "e.db")) as client:
        _admin(client)
        r = client.post("/filters", json={"name": "Everything"}, headers=H1)
        assert r.status_code == 201
        assert r.json()["criteria"] == {}


def test_duplicate_name_rejected(tmp_path):
    """Two filters can't share a name for one owner (case-insensitive)."""
    with TestClient(create_app(tmp_path / "d.db")) as client:
        _admin(client)
        client.post("/filters", json={"name": "Triage"}, headers=H1)
        assert (
            client.post("/filters", json={"name": "triage"}, headers=H1).status_code
            == 409
        )


def test_bad_priority_and_project_rejected(tmp_path):
    """Closed-set criteria are validated: an unreal priority or unparseable project
    is a 422, so a typo fails loud instead of silently matching nothing."""
    with TestClient(create_app(tmp_path / "v.db")) as client:
        _admin(client)
        assert (
            client.post(
                "/filters",
                json={"name": "x", "criteria": {"priority": "nope"}},
                headers=H1,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/filters",
                json={"name": "y", "criteria": {"project": "abc"}},
                headers=H1,
            ).status_code
            == 422
        )


def test_name_required(tmp_path):
    """A blank name is rejected at the boundary (422), matching the web form."""
    with TestClient(create_app(tmp_path / "n.db")) as client:
        _admin(client)
        assert (
            client.post("/filters", json={"name": "  "}, headers=H1).status_code == 422
        )


# --- API: running a filter --------------------------------------------------


def test_run_matches_priority_and_assignee(tmp_path):
    """Running a filter returns exactly the issues matching ALL criteria, and no
    others — the AND semantics that make a saved query trustworthy."""
    with TestClient(create_app(tmp_path / "r.db")) as client:
        _admin(client)
        hit = _issue(client, "urgent login bug", priority="high")
        _issue(client, "low chore", priority="low")
        other_high = _issue(client, "high but unassigned", priority="high")
        client.put(f"/issues/{hit['id']}/assignee", json={"assignee_id": 1}, headers=H1)
        fid = client.post(
            "/filters",
            json={
                "name": "mine-high",
                "criteria": {"priority": "high", "assignee_id": 1},
            },
            headers=H1,
        ).json()["id"]
        titles = [
            i["title"] for i in client.get(f"/filters/{fid}/issues", headers=H1).json()
        ]
        assert titles == ["urgent login bug"]
        assert other_high["title"] not in titles


def test_run_resolves_label_and_backlog(tmp_path):
    """A label criterion resolves to the issues carrying it; project='none' scopes
    to the backlog — both go through the shared list path, not a parallel one."""
    with TestClient(create_app(tmp_path / "rl.db")) as client:
        _admin(client)
        proj = client.post(
            "/projects", json={"name": "Web", "key": "WEB"}, headers=H1
        ).json()
        backlog_issue = _issue(client, "backlog item")
        in_project = _issue(client, "project item", project_id=proj["id"])
        label = client.post("/labels", json={"name": "ux"}, headers=H1).json()
        client.post(
            f"/issues/{backlog_issue['id']}/labels",
            json={"label_id": label["id"]},
            headers=H1,
        )
        # label filter
        fid = client.post(
            "/filters",
            json={"name": "ux-items", "criteria": {"label": "ux"}},
            headers=H1,
        ).json()["id"]
        assert [
            i["id"] for i in client.get(f"/filters/{fid}/issues", headers=H1).json()
        ] == [backlog_issue["id"]]
        # backlog filter
        bid = client.post(
            "/filters",
            json={"name": "backlog", "criteria": {"project": "none"}},
            headers=H1,
        ).json()["id"]
        ids = [i["id"] for i in client.get(f"/filters/{bid}/issues", headers=H1).json()]
        assert backlog_issue["id"] in ids and in_project["id"] not in ids


def test_run_carries_labels(tmp_path):
    """Run results are shaped like issue reads — labels composed on — so the client
    can render a filter's matches the same way it renders the issue list."""
    with TestClient(create_app(tmp_path / "rc.db")) as client:
        _admin(client)
        iss = _issue(client, "labeled")
        label = client.post("/labels", json={"name": "bug"}, headers=H1).json()
        client.post(
            f"/issues/{iss['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        fid = client.post("/filters", json={"name": "all"}, headers=H1).json()["id"]
        result = client.get(f"/filters/{fid}/issues", headers=H1).json()
        assert [label["name"]] == [
            lbl["name"] for i in result if i["id"] == iss["id"] for lbl in i["labels"]
        ]


# --- API: ownership ---------------------------------------------------------


def test_filters_are_private(tmp_path):
    """A filter is owner-scoped: another user can't list, read, run, change, or
    delete it — it reads as 404, never revealing it exists."""
    with TestClient(create_app(tmp_path / "p.db")) as client:
        _admin(client)
        _user2(client)
        fid = client.post("/filters", json={"name": "secret"}, headers=H1).json()["id"]
        # user2's list is empty; every path to user1's filter is a 404.
        assert client.get("/filters", headers=H2).json() == []
        assert client.get(f"/filters/{fid}", headers=H2).status_code == 404
        assert client.get(f"/filters/{fid}/issues", headers=H2).status_code == 404
        assert (
            client.patch(f"/filters/{fid}", json={"name": "x"}, headers=H2).status_code
            == 404
        )
        assert client.delete(f"/filters/{fid}", headers=H2).status_code == 404


def test_two_owners_share_a_name(tmp_path):
    """Per-owner uniqueness: two users may each have a filter with the same name."""
    with TestClient(create_app(tmp_path / "to.db")) as client:
        _admin(client)
        _user2(client)
        assert (
            client.post("/filters", json={"name": "Mine"}, headers=H1).status_code
            == 201
        )
        assert (
            client.post("/filters", json={"name": "Mine"}, headers=H2).status_code
            == 201
        )


def test_filters_require_authentication(tmp_path):
    """No actor at all → 401. Filters are personal; there is no anonymous inbox."""
    with TestClient(create_app(tmp_path / "au.db")) as client:
        _admin(client)
        assert client.get("/filters").status_code == 401
        assert client.post("/filters", json={"name": "x"}).status_code == 401


# --- API: update + delete ---------------------------------------------------


def test_patch_renames_and_replaces_criteria(tmp_path):
    """PATCH updates name and/or criteria; an empty fields body is a 422; renaming
    onto another of the owner's filters is a 409."""
    with TestClient(create_app(tmp_path / "pa.db")) as client:
        _admin(client)
        fid = client.post(
            "/filters",
            json={"name": "old", "criteria": {"priority": "low"}},
            headers=H1,
        ).json()["id"]
        client.post("/filters", json={"name": "taken"}, headers=H1)
        updated = client.patch(
            f"/filters/{fid}",
            json={"name": "new", "criteria": {"status": "open"}},
            headers=H1,
        ).json()
        assert updated["name"] == "new" and updated["criteria"] == {"status": "open"}
        assert client.patch(f"/filters/{fid}", json={}, headers=H1).status_code == 422
        assert (
            client.patch(
                f"/filters/{fid}", json={"name": "taken"}, headers=H1
            ).status_code
            == 409
        )


def test_delete_filter(tmp_path):
    """DELETE removes the filter; afterwards both the read and the run 404."""
    with TestClient(create_app(tmp_path / "del.db")) as client:
        _admin(client)
        fid = client.post("/filters", json={"name": "tmp"}, headers=H1).json()["id"]
        assert client.delete(f"/filters/{fid}", headers=H1).status_code == 204
        assert client.get(f"/filters/{fid}", headers=H1).status_code == 404
        assert client.get(f"/filters/{fid}/issues", headers=H1).status_code == 404


# --- API: the extended issue list (the dimensions a filter persists) --------


def test_issue_list_filters_by_priority_and_assignee(tmp_path):
    """GET /issues gained priority and assignee filters — the same direct-column
    dimensions a saved filter stores — so an ad-hoc query and a saved one match
    through one path."""
    with TestClient(create_app(tmp_path / "il.db")) as client:
        _admin(client)
        a = _issue(client, "high one", priority="high")
        _issue(client, "low one", priority="low")
        client.put(f"/issues/{a['id']}/assignee", json={"assignee_id": 1}, headers=H1)
        assert [
            i["title"] for i in client.get("/issues?priority=high", headers=H1).json()
        ] == ["high one"]
        assert [
            i["title"] for i in client.get("/issues?assignee=1", headers=H1).json()
        ] == ["high one"]


# --- web --------------------------------------------------------------------


def _login(client):
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


def test_web_create_list_and_summary(tmp_path):
    """The web form saves a filter for the session user and the list shows it with a
    human criteria summary that resolves ids to names (assignee → display name)."""
    with TestClient(create_app(tmp_path / "wc.db")) as client:
        _admin(client)
        csrf = _login(client)
        client.post(
            "/aegis/filters",
            data={"name": "High prio", "priority": "high", "assignee_id": "1"},
            headers={"X-CSRF-Token": csrf},
        )
        page = client.get("/aegis/filters").text
        assert "High prio" in page
        assert "priority: high" in page
        assert "assignee: Alice" in page


def test_web_summary_hides_a_private_project_name(tmp_path):
    # WHY: _describe_criteria renders a saved filter's `project` id as the project NAME,
    # and validate_criteria accepts any numeric id — so without an actor gate any user
    # could save `project: <private_id>` and read a hidden project's name back on
    # /aegis/filters. A viewer who can't see the project must get the id (#N), never the
    # name (the hidden==missing rule). The two web callers pass the session user through;
    # this pins the gate itself for member/admin/outsider/anonymous.
    from athena.aegis import projects
    from athena.core import db, users
    from athena.web.filters import _describe_criteria

    conn = db.connect(tmp_path / "leak.db")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('admin@e.com', 'Admin', 'admin')"
    )
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('c@e.com', 'Creator', 'member')"
    )
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('o@e.com', 'Outsider', 'member')"
    )
    conn.commit()
    proj = projects.create_project(conn, name="Acquisition", key="ACQ", created_by=2)
    conn.execute(
        "UPDATE projects SET visibility = 'private' WHERE id = ?", (proj["id"],)
    )
    conn.commit()
    crit = {"project": str(proj["id"])}

    # The creator (a member) and an admin (god-view) see the real name...
    assert (
        _describe_criteria(conn, crit, users.get_user(conn, 2))
        == "project: Acquisition"
    )
    assert (
        _describe_criteria(conn, crit, users.get_user(conn, 1))
        == "project: Acquisition"
    )
    # ...but an outsider and an anonymous viewer get only the id — no name leak.
    assert (
        _describe_criteria(conn, crit, users.get_user(conn, 3))
        == f"project: #{proj['id']}"
    )
    assert _describe_criteria(conn, crit, None) == f"project: #{proj['id']}"
    conn.close()


def test_web_run_shows_matches(tmp_path):
    """The web detail view runs the filter and lists matching issues (and excludes
    non-matching ones), through the same run path as the API."""
    with TestClient(create_app(tmp_path / "wr.db")) as client:
        _admin(client)
        csrf = _login(client)
        _issue(client, "match me", priority="high")
        _issue(client, "skip me", priority="low")
        client.post(
            "/aegis/filters",
            data={"name": "highs", "priority": "high"},
            headers={"X-CSRF-Token": csrf},
        )
        fid = client.get("/filters", headers=H1).json()[0]["id"]
        page = client.get(f"/aegis/filters/{fid}").text
        assert "match me" in page and "skip me" not in page


def test_web_save_current_view_link_and_prefill(tmp_path):
    """The issues list offers a 'Save current view' link carrying the active filters,
    and the filters form pre-fills from those query params — the one-click loop from
    an ad-hoc search to a saved filter."""
    with TestClient(create_app(tmp_path / "ws.db")) as client:
        _admin(client)
        _login(client)
        page = client.get("/aegis/issues?status=open&search=bug").text
        assert "Save current view" in page
        assert "/aegis/filters?status=open" in page
        form = client.get("/aegis/filters?name=foo&status=open&search=bug").text
        assert 'value="foo"' in form and 'value="bug"' in form


def test_web_dup_and_bad_input(tmp_path):
    """Web validation mirrors the API: duplicate name → 409, bad priority → 400."""
    with TestClient(create_app(tmp_path / "wd.db")) as client:
        _admin(client)
        csrf = _login(client)
        client.post(
            "/aegis/filters", data={"name": "dup"}, headers={"X-CSRF-Token": csrf}
        )
        assert (
            client.post(
                "/aegis/filters", data={"name": "dup"}, headers={"X-CSRF-Token": csrf}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/aegis/filters",
                data={"name": "z", "priority": "nope"},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 400
        )


def test_web_requires_login_and_ownership(tmp_path):
    """Logged out, the filters page is 401; a logged-in user can't open another
    user's filter (404)."""
    with TestClient(create_app(tmp_path / "wo.db")) as client:
        _admin(client)
        # user1 (Alice) is the only one who can log in (has a password); create her
        # filter via the API, then a second user owns a different one.
        _user2(client)
        other = client.post("/filters", json={"name": "bobs"}, headers=H2).json()["id"]
        assert client.get("/aegis/filters").status_code == 401  # logged out
        _login(client)  # now Alice
        assert client.get(f"/aegis/filters/{other}").status_code == 404


def test_web_delete(tmp_path):
    """The web delete removes the filter and returns to the list without it."""
    with TestClient(create_app(tmp_path / "wdel.db")) as client:
        _admin(client)
        csrf = _login(client)
        client.post(
            "/aegis/filters", data={"name": "gone"}, headers={"X-CSRF-Token": csrf}
        )
        fid = client.get("/filters", headers=H1).json()[0]["id"]
        client.post(f"/aegis/filters/{fid}/delete", headers={"X-CSRF-Token": csrf})
        assert "gone" not in client.get("/aegis/filters").text


# --- search within a saved filter -------------------------------------------


def test_search_within_filter_intersects_text_and_criteria(tmp_path):
    """A ?q= on the filter page narrows the filter's issues to those matching the
    text — honoring the WHOLE filter (here priority=high), so a low-priority issue
    that matches the text is still excluded, as is a high one that doesn't."""
    with TestClient(create_app(tmp_path / "swf.db")) as client:
        _admin(client)
        _login(client)
        _issue(client, "deploy pipeline", priority="high")  # match: text + priority
        _issue(client, "deploy docs", priority="low")  # text but wrong priority
        _issue(client, "unrelated thing", priority="high")  # priority but no text
        fid = client.post(
            "/filters",
            json={"name": "highs", "criteria": {"priority": "high"}},
            headers=H1,
        ).json()["id"]
        page = client.get(f"/aegis/filters/{fid}?q=deploy").text
        assert "deploy pipeline" in page
        assert "deploy docs" not in page  # excluded by the filter's priority
        assert "unrelated thing" not in page  # excluded by the text
        assert "result" in page and "within this filter" in page


def test_search_within_filter_keeps_saved_text_constraint(tmp_path):
    """The ad-hoc query narrows the saved result; it never replaces the filter's
    existing title/body substring constraint."""
    with TestClient(create_app(tmp_path / "swt.db")) as client:
        _admin(client)
        _login(client)
        _issue(client, "alpha common")
        _issue(client, "beta common")
        fid = client.post(
            "/filters",
            json={"name": "alphas", "criteria": {"search": "alpha"}},
            headers=H1,
        ).json()["id"]
        page = client.get(f"/aegis/filters/{fid}?q=common").text
        assert "alpha common" in page
        assert "beta common" not in page


def test_search_within_filter_blank_q_shows_full_run(tmp_path):
    """No query → the plain filter run (every matching issue), with the search box
    still offered."""
    with TestClient(create_app(tmp_path / "swb.db")) as client:
        _admin(client)
        _login(client)
        _issue(client, "alpha", priority="high")
        _issue(client, "beta", priority="high")
        fid = client.post(
            "/filters",
            json={"name": "highs", "criteria": {"priority": "high"}},
            headers=H1,
        ).json()["id"]
        page = client.get(f"/aegis/filters/{fid}").text
        assert "alpha" in page and "beta" in page
        assert 'name="q"' in page  # the "search within this filter" box is present


def test_search_within_filter_no_match_message(tmp_path):
    with TestClient(create_app(tmp_path / "swn.db")) as client:
        _admin(client)
        _login(client)
        _issue(client, "alpha", priority="high")
        fid = client.post(
            "/filters",
            json={"name": "highs", "criteria": {"priority": "high"}},
            headers=H1,
        ).json()["id"]
        page = client.get(f"/aegis/filters/{fid}?q=zzznope").text
        assert "No issues in this filter match" in page
