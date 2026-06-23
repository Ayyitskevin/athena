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
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=h).json()
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
            "/issues", json={"title": "secret thing", "body": ""},
            headers={"X-Athena-Actor": "1"},
        )
        body = client.get("/find").text
        assert "secret thing" not in body
        assert "search every issue and page" in body


def test_find_no_match_says_so(tmp_path):
    app = create_app(tmp_path / "nomatch.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "nomatch.db")
        client.post(
            "/issues", json={"title": "apples", "body": ""},
            headers={"X-Athena-Actor": "1"},
        )
        body = client.get("/find", params={"q": "zzzznothing"}).text
        assert "No matches" in body


def test_find_is_open_without_login(tmp_path):
    # WHY: web reads are uniformly open; the gate lives on the JSON API surface.
    app = create_app(tmp_path / "open.db")
    with TestClient(app) as client:
        assert client.get("/find", params={"q": "anything"}).status_code == 200
