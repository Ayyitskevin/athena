"""The issue list's priority + assignee filters.

S9 gave issues.list_issues (and GET /issues) priority/assignee dimensions; this
surfaces them on the web list. These tests pin the contract:

  * each filter narrows the rendered list and the select echoes the active choice;
  * the web list and the API agree on what a priority/assignee filter matches (the
    cardinal rule — both go through list_issues, so neither can drift);
  * the filters ride along through pagination and into the "Save current view" link
    (mapped to the saved-filter form's field names), so they aren't lost on the
    next click.
"""
from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})


def _bob(client):
    client.post("/users", json={"email": "b@e.com", "name": "Bob"}, headers=H1)  # id 2


def _issue(client, title, **kw):
    return client.post("/issues", json={"title": title, **kw}, headers=H1).json()


def test_list_filters_by_priority(tmp_path):
    with TestClient(create_app(tmp_path / "p.db")) as client:
        _admin(client)
        _issue(client, "high one", priority="high")
        _issue(client, "low two", priority="low")
        body = client.get("/aegis/issues?priority=high").text
        assert "high one" in body and "low two" not in body
        # the select is present and echoes the active choice
        assert 'name="priority"' in body
        assert '<option value="high" selected>' in body


def test_list_filters_by_assignee(tmp_path):
    with TestClient(create_app(tmp_path / "a.db")) as client:
        _admin(client)
        _bob(client)
        a = _issue(client, "bobs task")
        _issue(client, "nobody task")
        client.put(f"/issues/{a['id']}/assignee", json={"assignee_id": 2}, headers=H1)
        body = client.get("/aegis/issues?assignee=2").text
        assert "bobs task" in body and "nobody task" not in body
        assert '<option value="2" selected>Bob</option>' in body


def test_priority_and_assignee_align_web_and_api(tmp_path):
    # WHY: the list and GET /issues must match the same rows for the same filter —
    # they share list_issues, so this pins they can't diverge.
    with TestClient(create_app(tmp_path / "al.db")) as client:
        _admin(client)
        _bob(client)
        a = _issue(client, "alpha", priority="high")
        _issue(client, "beta", priority="low")
        client.put(f"/issues/{a['id']}/assignee", json={"assignee_id": 2}, headers=H1)

        api_titles = {r["title"] for r in client.get("/issues?priority=high").json()}
        assert api_titles == {"alpha"}
        web = client.get("/aegis/issues?priority=high").text
        assert "alpha" in web and "beta" not in web

        api_assignee = {r["title"] for r in client.get("/issues?assignee=2").json()}
        assert api_assignee == {"alpha"}
        web_assignee = client.get("/aegis/issues?assignee=2").text
        assert "alpha" in web_assignee and "beta" not in web_assignee


def test_filters_survive_pagination(tmp_path):
    # WHY: paging must not drop the active priority/assignee filter — the Next link
    # has to carry them or page 2 silently shows everything.
    with TestClient(create_app(tmp_path / "pg.db")) as client:
        _admin(client)
        for i in range(25):
            _issue(client, f"hi {i}", priority="high")
        body = client.get("/aegis/issues?priority=high&per_page=10").text
        # the pagination Next link (and every filter control) carries the filter
        assert "priority=high" in body


def test_save_current_view_carries_priority_and_assignee(tmp_path):
    # WHY: "Save current view" should capture the whole active view, including the
    # new dimensions — mapped to the saved-filter form's field names.
    with TestClient(create_app(tmp_path / "sv.db")) as client:
        _admin(client)
        _bob(client)
        client.post("/login", data={"email": "a@e.com", "password": "pw"})
        body = client.get("/aegis/issues?priority=high&assignee=2").text
        assert "priority=high" in body
        assert "assignee_id=2" in body  # mapped to the create form's field name


def test_unknown_priority_matches_nothing_like_the_api(tmp_path):
    # WHY: an unknown priority is passed straight through (matches nothing), the same
    # as GET /issues — the web doesn't invent a different meaning for bad input.
    with TestClient(create_app(tmp_path / "u.db")) as client:
        _admin(client)
        _issue(client, "real issue", priority="high")
        assert client.get("/issues?priority=nope").json() == []
        body = client.get("/aegis/issues?priority=nope").text
        assert "real issue" not in body
