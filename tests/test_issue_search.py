"""Filtered issue search — full-text relevance narrowed by structured filters.

This is the synthesis of saved filters (structured) and full-text search (ranked):
a result must BOTH match the query AND satisfy every filter. These tests pin down
that intersection, the things it must NOT change, and the boundaries:

  * the filter set comes from the one issue-filtering path (list_issues), so a
    filtered search agrees with an ad-hoc filter on what qualifies — and ranking
    comes from core.search, so it agrees with a plain search on order;
  * it is issue-only (a page that matches the text is excluded — pages have no
    status/label/project);
  * a blank query returns nothing (search, not browse), and an unparseable project
    filter is rejected;
  * the REST endpoint is reachable at /issues/search (it does not collide with the
    /issues/{ref} read), and the web /find drops into filtered issue mode when any
    filter is set.
"""

from athena.aegis import issue_search, issues, projects
from athena.core import db, labels
from athena.main import create_app
from athena.mentor import pages, spaces
from fastapi.testclient import TestClient

H = {"X-Athena-Actor": "1"}


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    return conn


# --- unit: the intersection -------------------------------------------------


def test_blank_query_returns_nothing(tmp_path):
    conn = _conn(tmp_path / "b.db")
    issues.create_issue(conn, title="deploy thing", body="", created_by=1)
    assert issue_search.search_issues(conn, "") == []
    assert issue_search.search_issues(conn, "   ", status="open") == []


def test_unfiltered_is_a_plain_issue_search(tmp_path):
    # WHY: with no filter set it must behave exactly like an issue-scoped search —
    # ranked, and excluding pages.
    conn = _conn(tmp_path / "u.db")
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    iss = issues.create_issue(conn, title="deploy pipeline", body="", created_by=1)
    pages.create_page(
        conn, space_id=sp["id"], title="deploy runbook", body="", created_by=1
    )
    hits = issue_search.search_issues(conn, "deploy")
    assert [(h["kind"], h["source_id"]) for h in hits] == [("issue", iss["id"])]


def test_status_filter_narrows_and_keeps_rank(tmp_path):
    # WHY: the filter constrains, the FTS orders. Two issues match the text; the
    # status filter keeps only the one that also matches, and it stays an issue hit
    # with its enriched status.
    conn = _conn(tmp_path / "s.db")
    a = issues.create_issue(conn, title="deploy alpha", body="", created_by=1)
    issues.create_issue(conn, title="deploy beta", body="", created_by=1)
    issues.update_issue(conn, a["id"], status="done")
    hits = issue_search.search_issues(conn, "deploy", status="done")
    assert [h["source_id"] for h in hits] == [a["id"]]
    assert hits[0]["status"] == "done"


def test_label_filter_intersects(tmp_path):
    conn = _conn(tmp_path / "l.db")
    a = issues.create_issue(conn, title="deploy alpha", body="", created_by=1)
    issues.create_issue(conn, title="deploy beta", body="", created_by=1)
    lab = labels.create_label(conn, name="infra")
    labels.add_label_to_issue(conn, a["id"], lab["id"])
    hits = issue_search.search_issues(conn, "deploy", label="infra")
    assert [h["source_id"] for h in hits] == [a["id"]]


def test_project_filter_intersects(tmp_path):
    conn = _conn(tmp_path / "p.db")
    proj = projects.create_project(conn, name="Web", key="WEB", created_by=1)
    inproj = issues.create_issue(
        conn, title="deploy alpha", body="", created_by=1, project_id=proj["id"]
    )
    backlog = issues.create_issue(conn, title="deploy beta", body="", created_by=1)
    assert [
        h["source_id"]
        for h in issue_search.search_issues(conn, "deploy", project=str(proj["id"]))
    ] == [inproj["id"]]
    assert [
        h["source_id"]
        for h in issue_search.search_issues(conn, "deploy", project="none")
    ] == [backlog["id"]]


def test_filter_matching_nothing_returns_empty(tmp_path):
    # WHY: a filter that excludes every textual match yields [], not a fallback to
    # the unfiltered results.
    conn = _conn(tmp_path / "n.db")
    issues.create_issue(
        conn, title="deploy alpha", body="", created_by=1, priority="low"
    )
    assert issue_search.search_issues(conn, "deploy", priority="urgent") == []


# --- API --------------------------------------------------------------------


def test_endpoint_does_not_collide_with_issue_ref(tmp_path):
    # WHY: /issues/search must resolve to search, not be swallowed by /issues/{ref}
    # as ref="search". Both must work.
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        iss = client.post(
            "/issues", json={"title": "deploy pipeline"}, headers=H
        ).json()
        r = client.get("/issues/search", params={"q": "deploy"})
        assert r.status_code == 200
        assert [h["source_id"] for h in r.json()] == [iss["id"]]
        # the ref read still works
        assert client.get(f"/issues/{iss['id']}").json()["title"] == "deploy pipeline"


def test_endpoint_filters_and_excludes_pages(tmp_path):
    app = create_app(tmp_path / "apif.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        a = client.post(
            "/issues", json={"title": "deploy alpha", "priority": "high"}, headers=H
        ).json()
        client.post(
            "/issues", json={"title": "deploy beta", "priority": "low"}, headers=H
        )
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H
        ).json()
        client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "deploy runbook"}, headers=H
        )
        # no pages, even though a page matches the text
        kinds = {
            h["kind"]
            for h in client.get("/issues/search", params={"q": "deploy"}).json()
        }
        assert kinds == {"issue"}
        # priority filter narrows to the one matching issue
        hi = client.get(
            "/issues/search", params={"q": "deploy", "priority": "high"}
        ).json()
        assert [h["source_id"] for h in hi] == [a["id"]]


def test_endpoint_rejects_bad_project_filter(tmp_path):
    app = create_app(tmp_path / "apibad.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        assert (
            client.get(
                "/issues/search", params={"q": "x", "project": "abc"}
            ).status_code
            == 422
        )


def test_endpoint_bounds_limit_and_offset(tmp_path):
    # WHY: this endpoint is anonymously reachable (optional_actor) and no per-token rate
    # limit covers the anon path, so an unbounded limit is a DoS lever — `limit=-1` would
    # reach SQLite as `LIMIT -1` and disable the cap, materializing every visible hit.
    # Bounds must match the core /search endpoint (Query(20, ge=1, le=100)).
    app = create_app(tmp_path / "apibound.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        assert (
            client.get("/issues/search", params={"q": "x", "limit": -1}).status_code
            == 422
        )
        assert (
            client.get("/issues/search", params={"q": "x", "limit": 0}).status_code
            == 422
        )
        assert (
            client.get("/issues/search", params={"q": "x", "limit": 1000}).status_code
            == 422
        )
        assert (
            client.get("/issues/search", params={"q": "x", "offset": -1}).status_code
            == 422
        )
        assert (
            client.get(
                "/issues/search",
                params={"q": "x", "offset": issues.MAX_OFFSET},
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/issues/search",
                params={"q": "x", "offset": issues.MAX_OFFSET + 1},
            ).status_code
            == 422
        )
        assert (
            client.get("/issues/search", params={"q": "x", "limit": 50}).status_code
            == 200
        )


# --- web --------------------------------------------------------------------


def test_find_enters_filtered_issue_mode(tmp_path):
    # WHY: setting a filter on /find switches to filtered issue search — only ranked
    # matching issues, pages dropped, scope tabs replaced by the filtered note.
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        a = client.post("/issues", json={"title": "deploy alpha"}, headers=H).json()
        client.post("/issues", json={"title": "deploy beta"}, headers=H)
        client.patch(f"/issues/{a['id']}", json={"status": "done"}, headers=H)
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H
        ).json()
        client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "deploy runbook"}, headers=H
        )

        # plain search shows scope tabs and the page hit
        plain = client.get("/find", params={"q": "deploy"}).text
        assert "search-scope" in plain and "deploy runbook" in plain
        # filtered: issue-only, pages and non-matching issue gone, tabs replaced
        filtered = client.get("/find", params={"q": "deploy", "status": "done"}).text
        assert "Showing issues that match the filters" in filtered
        assert "search-scope" not in filtered
        assert "deploy alpha" in filtered
        assert "deploy beta" not in filtered and "deploy runbook" not in filtered


def test_find_bad_project_is_400(tmp_path):
    app = create_app(tmp_path / "web400.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A"})
        assert (
            client.get("/find", params={"q": "x", "project": "abc"}).status_code == 400
        )
