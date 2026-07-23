"""The web issue list/boards and the REST API must agree on what a filter MEANS.

These are regression tests for a real divergence cluster: the browser routes in
web/router.py had grown their OWN copies of the filter logic — a separate project
parser, a pre-lowered search needle, an unbounded sort key, a 200-with-error
not-found page — each of which could (and did) drift from the API that is supposed
to be the single source of truth. The cardinal rule (web is a thin client over the
API) is only true if the two surfaces can't disagree, so each test here pins one
way they used to:

  * parse_project_filter is ONE parser, shared — a garbled ?project= is rejected
    on BOTH surfaces (web 400, API 422), never silently widened to "all issues";
  * search casing is owned by SQLite LIKE, not pre-lowered in the web layer, so an
    uppercase needle matches the same rows through the list, the boards, and the API;
  * an unknown ?sort= column falls back to created_at instead of raising;
  * a missing issue detail is a real 404 AND tells the human why (renders the
    error context), not the old 200-with-silent-empty-list;
  * the boards column filtering runs through list_issues, not a re-implemented
    Python scan.
"""

from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import db
from athena.main import create_app


def _seed_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("k@e.com", "K"))
    conn.commit()
    conn.close()


_H = {"X-Athena-Actor": "1"}


def _make_issue(client, title, body=""):
    return client.post(
        "/issues", json={"title": title, "body": body}, headers=_H
    ).json()


# --- unit: the shared parser is the one owner of ?project= meaning -----------


def test_parse_project_filter_valid_shapes():
    assert issues.parse_project_filter(None) == (None, False)
    assert issues.parse_project_filter("") == (None, False)
    assert issues.parse_project_filter("   ") == (None, False)
    assert issues.parse_project_filter("none") == (None, True)
    assert issues.parse_project_filter("NONE") == (None, True)  # case-insensitive
    assert issues.parse_project_filter("42") == (42, False)
    assert issues.parse_filter_id("0") == 0
    assert issues.parse_filter_id(str(issues.MAX_SQLITE_INTEGER)) == (
        issues.MAX_SQLITE_INTEGER
    )


def test_parse_project_filter_invalid_is_none_not_a_tuple():
    # WHY: invalid must be DISTINGUISHABLE from the valid no-filter case (None, False)
    # so callers reject it instead of unpacking garbage into "show everything".
    assert issues.parse_project_filter("abc") is None
    assert issues.parse_project_filter("1x") is None
    assert issues.parse_project_filter("²") is None
    assert issues.parse_project_filter(str(1 << 63)) is None


# --- web↔API agree: invalid project filter fails loud on both ----------------


def test_invalid_project_filter_web_400_api_422(tmp_path):
    # WHY: a garbled filter used to be silently treated as "all projects" on the
    # web side — a filter that lies. Now both surfaces refuse it.
    db_file = tmp_path / "f.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _make_issue(client, "anything")
        assert client.get("/aegis/issues?project=bogus").status_code == 400
        assert client.get("/issues?project=bogus").status_code == 422


def test_numeric_issue_filters_are_safe_at_every_http_boundary(tmp_path):
    # WHY: FastAPI can parse integers larger than SQLite can bind, while Python's
    # Unicode digit predicates accept characters int() cannot convert. REST rejects
    # invalid ids; the legacy web read treats them as an omitted filter, never a 500.
    db_file = tmp_path / "numeric-bounds.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _make_issue(client, "still visible")

        for field_name in ("assignee", "sprint"):
            assert (
                client.get(
                    "/issues",
                    params={field_name: issues.MAX_SQLITE_INTEGER},
                ).status_code
                == 200
            )
            for malformed in ("²", str(1 << 63), "-1"):
                assert (
                    client.get("/issues", params={field_name: malformed}).status_code
                    == 422
                )
                web = client.get("/aegis/issues", params={field_name: malformed})
                assert web.status_code == 200
                assert "still visible" in web.text


# --- web↔API agree: search casing is LIKE's job, not a pre-lower -------------


def test_uppercase_search_matches_same_rows_web_and_api(tmp_path):
    # WHY: the web layer used to pre-lower the needle; the API passed it raw. For
    # ASCII they happened to agree (LIKE is case-insensitive), but the duplicated
    # lowering was a latent divergence. Pin that an uppercase query finds the same
    # lowercase-titled issue on both surfaces.
    db_file = tmp_path / "s.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _make_issue(client, "apple pie")
        _make_issue(client, "banana bread")

        api_titles = {row["title"] for row in client.get("/issues?search=APPLE").json()}
        assert api_titles == {"apple pie"}

        web = client.get("/aegis/issues?search=APPLE").text
        assert "apple pie" in web
        assert "banana bread" not in web


# --- web: unknown sort column falls back, doesn't explode --------------------


def test_unknown_sort_falls_back_to_created_at(tmp_path):
    # WHY: an arbitrary ?sort= used to hit a bare except and silently pass; now an
    # off-whitelist column is normalized to created_at and the page still renders.
    db_file = tmp_path / "so.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _make_issue(client, "only issue")
        r = client.get("/aegis/issues?sort=DROP+TABLE")
        assert r.status_code == 200
        assert "only issue" in r.text


# --- web: missing issue detail is a real 404 ---------------------------------


def test_missing_issue_detail_is_404(tmp_path):
    db_file = tmp_path / "nd.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        # The contract this pins is the STATUS: a missing issue is a 404, matching
        # the API's /issues/9999 -> 404, not the old 200-with-empty-list that made
        # "gone" indistinguishable from "found" to a caller checking the code.
        r = client.get("/aegis/issues/9999")
        assert r.status_code == 404
        # ...and a HUMAN looking at the page must actually be told why it's empty.
        # The route passes an `error` context; this pins that the template renders
        # it, not silently drops it (the old dead-context gap).
        assert "9999 not found" in r.text
        assert client.get("/issues/9999").status_code == 404  # API agrees


# --- web: boards filters through list_issues ---------------------------------


def test_boards_filters_via_list_issues(tmp_path):
    # WHY: boards used to scan the whole table and re-filter in Python (its own
    # pre-lowered substring match). Now status + search go through list_issues, so
    # a search shows only matching cards on the board exactly as the list would.
    db_file = tmp_path / "b.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _make_issue(client, "kafka consumer lag")
        _make_issue(client, "ui button color")
        board = client.get("/aegis/boards?search=KAFKA").text
        assert "kafka consumer lag" in board
        assert "ui button color" not in board
