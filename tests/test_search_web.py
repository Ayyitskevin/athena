"""The web search surface (/find) — the browser twin of the JSON /search API.

These tests encode what matters for the human page, not just a 200:

  * one query returns ranked hits from BOTH kinds, each linking to its real
    detail page (issues -> /aegis/issues/N, pages -> /mentor/pages/N) — the web
    page owns no data, it renders what core.search returns;
  * the matched term is highlighted (<mark>) and author text is escaped first, so
    a body full of HTML can never break out into live markup (the XSS rule);
  * a blank box shows the prompt (it does not run a query or dump the table) and a
    no-match query says so;
  * reading is open — no login required, matching every other web read.
"""

from fastapi.testclient import TestClient

from athena.core import search
from athena.main import create_app


def _seed_user(db_file):
    from athena.core import db

    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    conn.close()


def test_find_renders_ranked_hits_across_kinds_with_links(tmp_path):
    app = create_app(tmp_path / "find.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "find.db")
        h = {"X-Athena-Actor": "1"}
        iss = client.post(
            "/issues", json={"title": "Telemetry export", "body": "metrics"}, headers=h
        ).json()
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=h
        ).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages",
            json={"title": "Telemetry guide", "body": "dashboards"},
            headers=h,
        ).json()

        body = client.get("/find", params={"q": "telemetry"}).text
        assert "Telemetry export" in body
        assert "Telemetry guide" in body
        assert f'href="/aegis/issues/{iss["id"]}"' in body
        assert f'href="/mentor/pages/{pg["id"]}"' in body


def test_find_highlights_match_and_escapes_html(tmp_path):
    # WHY: the snippet is excerpted from author text. The matched term must be
    # wrapped in <mark>, but any HTML in the body must be inert — escaped, never
    # rendered. A body carrying a <script> tag proves both at once.
    app = create_app(tmp_path / "xss.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "xss.db")
        h = {"X-Athena-Actor": "1"}
        client.post(
            "/issues",
            json={"title": "t", "body": "zzdanger <script>bad()</script> tail"},
            headers=h,
        )
        body = client.get("/find", params={"q": "zzdanger"}).text
        assert "<mark>zzdanger</mark>" in body  # the match is highlighted
        assert "<script>bad()" not in body  # the author's tag never goes live
        assert "&lt;script&gt;" in body  # it shows as inert, escaped text


def test_find_blank_query_shows_prompt_not_results(tmp_path):
    # WHY: an empty search box is "nothing to show", not "dump everything".
    app = create_app(tmp_path / "blank.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "blank.db")
        client.post(
            "/issues",
            json={"title": "secret thing", "body": ""},
            headers={"X-Athena-Actor": "1"},
        )
        body = client.get("/find").text
        assert "secret thing" not in body
        assert "search every visible issue, page, and room event" in body


def test_find_no_match_says_so(tmp_path):
    app = create_app(tmp_path / "nomatch.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "nomatch.db")
        client.post(
            "/issues",
            json={"title": "apples", "body": ""},
            headers={"X-Athena-Actor": "1"},
        )
        body = client.get("/find", params={"q": "zzzznothing"}).text
        assert "No matches" in body


def test_find_is_open_without_login(tmp_path):
    # WHY: web reads are uniformly open; the gate lives on the JSON API surface.
    app = create_app(tmp_path / "open.db")
    with TestClient(app) as client:
        assert client.get("/find", params={"q": "anything"}).status_code == 200


def test_find_shows_scope_filter_and_context(tmp_path):
    # WHY: a result must be triageable from the list. An issue hit shows its key and
    # status; a page hit shows its space; and a scope filter lets the searcher narrow
    # to one kind.
    app = create_app(tmp_path / "ctx.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "ctx.db")
        h = {"X-Athena-Actor": "1"}
        proj = client.post(
            "/projects", json={"name": "Web", "key": "WEB"}, headers=h
        ).json()
        iss = client.post(
            "/issues",
            json={"title": "obelisk export", "project_id": proj["id"]},
            headers=h,
        ).json()
        client.patch(f"/issues/{iss['id']}", json={"status": "done"}, headers=h)
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=h
        ).json()
        client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "obelisk guide"}, headers=h
        )

        body = client.get("/find", params={"q": "obelisk"}).text
        assert "search-scope" in body  # the All/Issues/Pages filter is present
        assert "WEB-1" in body  # issue key
        assert 'class="status done"' in body  # issue status badge
        assert ">ENG<" in body  # page's space key


def test_find_scope_narrows_to_one_kind(tmp_path):
    # WHY: choosing the Issues scope must drop page hits (and vice versa), the web
    # mirror of the API's kind filter.
    app = create_app(tmp_path / "scope.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "scope.db")
        h = {"X-Athena-Actor": "1"}
        client.post("/issues", json={"title": "widget alpha"}, headers=h)
        sp = client.post(
            "/spaces", json={"key": "ENG", "name": "Eng"}, headers=h
        ).json()
        client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "widget beta"}, headers=h
        )
        body = client.get("/find", params={"q": "widget", "kind": "issue"}).text
        assert "widget alpha" in body and "widget beta" not in body


def test_find_paginates_when_results_overflow_a_page(tmp_path):
    # WHY: search must not silently cap at one window. With more than a page of hits,
    # page 1 offers Next and page 2 offers Prev — the result set is walkable.
    app = create_app(tmp_path / "pagi.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "pagi.db")
        h = {"X-Athena-Actor": "1"}
        for i in range(21):  # one more than the 20-per-page window
            client.post("/issues", json={"title": f"falcon {i}"}, headers=h)
        page1 = client.get("/find", params={"q": "falcon"}).text
        assert "Next →" in page1
        assert "← Prev" not in page1
        page2 = client.get("/find", params={"q": "falcon", "page": 2}).text
        assert "← Prev" in page2
        assert "Next →" not in page2  # only 21 hits, so page 2 is the last


def test_find_clamps_pages_before_sqlite_offset_overflow(tmp_path):
    # WHY: /find intentionally forgives hand-edited page values. Preserve that
    # browser contract while keeping both plain and filtered search inside SQLite's
    # signed integer range instead of returning a 500 for a huge numeric page.
    app = create_app(tmp_path / "page-bound.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "page-bound.db")
        client.post(
            "/issues",
            json={"title": "falcon"},
            headers={"X-Athena-Actor": "1"},
        )
        oversized_page = search.MAX_OFFSET // 20 + 2
        for extra in ({}, {"status": "open"}):
            response = client.get(
                "/find",
                params={"q": "falcon", "page": oversized_page, **extra},
            )
            assert response.status_code == 200
            assert "No matches" in response.text
