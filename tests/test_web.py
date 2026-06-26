"""Tests for the web (HTML) layer.

Covers the foundation: GET / renders and the templates are wired.
Uses the real templates/ and static/ at project root.
"""

from html.parser import HTMLParser

from fastapi.testclient import TestClient

from athena.aegis import projects
from athena.core import db
from athena.main import create_app
from athena.mentor import spaces


class _FormAttrParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.forms.append(dict(attrs))


def _form_attrs(html: str) -> list[dict]:
    parser = _FormAttrParser()
    parser.feed(html)
    return parser.forms


def test_home_returns_200_and_contains_athena(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Athena" in response.text
    # Basic sanity that it's HTML and our layout is there
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()
    assert '<script src="/static/htmx.min.js"></script>' in response.text
    assert '<script src="/static/confirm.js"></script>' in response.text
    assert "unpkg.com" not in response.text


def test_aegis_dashboard_renders(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        client.post(
            "/issues",
            json={"title": "Dashboard test issue"},
            headers={"X-Athena-Actor": "1"},
        )
        response = client.get("/aegis")
    assert response.status_code == 200
    assert "Aegis" in response.text
    assert "Issues" in response.text
    assert "/aegis/issues" in response.text
    assert "Dashboard test issue" in response.text or "Recent Issues" in response.text
    assert "Issues (" in response.text  # count from status


def _seed_user(db_file):
    # Seed directly (no user API yet); then use /issues API to create real issue for web tests.
    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO users (email, name) VALUES (?, ?)", ("kevin@example.com", "Kevin")
    )
    conn.commit()
    conn.close()


def test_issues_list_renders_real_data(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        # seed one real issue via the API (actor identifies the creator)
        client.post(
            "/issues",
            json={"title": "Real issue from API", "body": "test"},
            headers={"X-Athena-Actor": "1"},
        )

        response = client.get("/aegis/issues")
    assert response.status_code == 200
    assert "Issues" in response.text
    assert "Real issue from API" in response.text
    assert 'hx-get="/aegis/issues?page=1' in response.text


def test_issue_table_hx_urls_are_encoded(tmp_path):
    # WHY: issue-list sort and pagination links carry user-controlled filters.
    # Server-built URLs must encode characters like spaces and ampersands instead
    # of letting them split or mutate the query string.
    db_file = tmp_path / "web_encoded_urls.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        issue = client.post(
            "/issues",
            json={"title": "alpha & beta"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        label = client.post(
            "/labels",
            json={"name": "bug & ux"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": label["id"]},
            headers={"X-Athena-Actor": "1"},
        )

        response = client.get(
            "/aegis/issues?label=bug+%26+ux&search=alpha+%26+beta",
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "label=bug+%26+ux" in response.text
    assert "search=alpha+%26+beta" in response.text
    assert "label=bug & ux" not in response.text
    assert "search=alpha & beta" not in response.text


def test_project_delete_confirm_keeps_name_out_of_inline_javascript(tmp_path):
    # WHY: project names are untrusted text. They may appear in confirmation
    # messages, but never inside an inline JavaScript string.
    db_file = tmp_path / "project_confirm_xss.db"
    app = create_app(db_file)
    payload = "x');alert(1);//"
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@example.com", "name": "A", "password": "secret"},
        )
        client.post("/login", data={"email": "a@example.com", "password": "secret"})
        conn = db.connect(db_file)
        projects.create_project(conn, name=payload, key="X", created_by=1)
        conn.close()

        response = client.get("/aegis/projects/1/edit")

    assert response.status_code == 200
    forms = _form_attrs(response.text)
    delete_form = next(
        form for form in forms if form.get("action") == "/aegis/projects/1/delete"
    )
    assert "onsubmit" not in delete_form
    assert delete_form["data-confirm"] == (
        "Delete project x');alert(1);//? This cannot be undone."
    )


def test_space_delete_confirm_keeps_name_out_of_inline_javascript(tmp_path):
    # WHY: space names flow through the same confirm UI as projects and need the
    # same JS-context protection.
    db_file = tmp_path / "space_confirm_xss.db"
    app = create_app(db_file)
    payload = "x');alert(1);//"
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@example.com", "name": "A", "password": "secret"},
        )
        client.post("/login", data={"email": "a@example.com", "password": "secret"})
        conn = db.connect(db_file)
        spaces.create_space(conn, key="X", name=payload, created_by=1)
        conn.close()

        response = client.get("/mentor/spaces/1")

    assert response.status_code == 200
    forms = _form_attrs(response.text)
    delete_form = next(
        form for form in forms if form.get("action") == "/mentor/spaces/1/delete"
    )
    assert "onsubmit" not in delete_form
    assert delete_form["data-confirm"] == (
        "Delete space x');alert(1);//? This cannot be undone."
    )


def test_issue_detail_renders_real_data(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        created = client.post(
            "/issues",
            json={"title": "Detail test issue"},
            headers={"X-Athena-Actor": "1"},
        )
        issue_id = created.json()["id"]

        response = client.get(f"/aegis/issues/{issue_id}")
    assert response.status_code == 200
    assert f"#{issue_id}" in response.text
    assert "Detail test issue" in response.text
    assert "Back to issues" in response.text


def test_new_issue_form_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/issues/new")
    assert response.status_code == 200
    assert "New Issue" in response.text
    assert 'hx-post="/aegis/issues"' in response.text
    assert 'name="title"' in response.text


def test_create_issue_blocked_when_logged_out(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        # No session cookie → the UI write path is refused with a sign-in prompt,
        # the same actor rule the REST API enforces (see test_auth.py for the
        # logged-in counterpart).
        response = client.post(
            "/aegis/issues",
            data={"title": "Should not create", "body": "foo", "status": "open"},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 401
    assert "sign in" in response.text.lower()


def test_assignee_name_shows_in_list_and_board(tmp_path):
    # WHY: the assignee's name (resolved by the issues.py LEFT JOIN) must reach
    # the list table AND the board cards, not just the detail page — otherwise
    # "who's on it" is invisible exactly where you scan many issues at once.
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)  # user 1 = Kevin
        created = client.post(
            "/issues", json={"title": "assigned issue"}, headers={"X-Athena-Actor": "1"}
        )
        issue_id = created.json()["id"]
        client.put(
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": 1},
            headers={"X-Athena-Actor": "1"},
        )

        list_view = client.get("/aegis/issues")
        assert list_view.status_code == 200
        assert "Assignee" in list_view.text  # column header
        assert "Kevin" in list_view.text  # the resolved name, not just an id

        board_view = client.get("/aegis/boards")
        assert board_view.status_code == 200
        assert "Kevin" in board_view.text


def test_unassigned_issue_reads_unassigned_in_list(tmp_path):
    # WHY: an unassigned issue must say so explicitly, not render a blank cell
    # that looks like a bug or missing data.
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        client.post(
            "/issues", json={"title": "nobody's issue"}, headers={"X-Athena-Actor": "1"}
        )
        response = client.get("/aegis/issues")
    assert response.status_code == 200
    assert "Unassigned" in response.text


def test_project_name_shows_on_board_card(tmp_path):
    # WHY: when scanning a board you want to see which project a card belongs to
    # without opening it. The project name (resolved by the issues.py LEFT JOIN)
    # must reach the board card, and link to that project's filtered list.
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)  # user 1 = Kevin
        proj = client.post(
            "/projects",
            json={"name": "Atlas", "key": "ATL"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        client.post(
            "/issues",
            json={"title": "in atlas", "project_id": proj["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        client.post(
            "/issues", json={"title": "loose card"}, headers={"X-Athena-Actor": "1"}
        )

        board = client.get("/aegis/boards")
        assert board.status_code == 200
        # the projected card shows the project name, linking to its filtered list
        assert "Atlas" in board.text
        assert f"/aegis/issues?project={proj['id']}" in board.text
        # a backlog issue gets no project chip — the name is the only "Atlas" hit,
        # so the count of project links equals the one projected card
        assert board.text.count('class="project"') == 1


def test_boards_page_renders(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        response = client.get("/aegis/boards")
    assert response.status_code == 200
    assert "Boards" in response.text
    assert "Kanban" in response.text or "board" in response.text.lower()
    # Board columns are per-project/dynamic now, so an EMPTY board has no status
    # columns; the page chrome (the status filter) still renders.
    assert "All statuses" in response.text


def test_issues_list_htmx_fragment_vs_full_page(tmp_path):
    db_file = tmp_path / "web.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        client.post(
            "/issues",
            json={"title": "Searchable real issue", "body": "test"},
            headers={"X-Athena-Actor": "1"},
        )

        # HTMX request (fragment only)
        response = client.get(
            "/aegis/issues?search=Searchable",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Searchable real issue" in response.text
        # Fragment: has table content but NOT page chrome (search input, header, nav etc.)
        assert "search-input" not in response.text
        assert "page-header" not in response.text.lower()
        assert '<div id="issues-table">' in response.text

        # Non-HTMX: full page
        response = client.get("/aegis/issues?search=Searchable")
        assert response.status_code == 200
        assert "Searchable real issue" in response.text
        assert "search-input" in response.text  # page chrome present
        assert "Issues" in response.text  # from <h1> or title in full page
