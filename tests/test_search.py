"""Unified full-text search across issues and pages — the second one-roof payoff.

These tests encode the contract that matters, not just that the FTS query runs:

  * writing an issue or page makes it findable (the data layer keeps the index
    current), and a single query ranks hits from BOTH modules together — the whole
    point of one shared database;
  * a title hit outranks a body-only hit (bm25 column weighting), so the most
    relevant result leads;
  * search is prefix-matched ("desig" finds "design") and term-ANDed;
  * an edit re-indexes — a removed word stops matching, a new title becomes
    searchable — so the index never lies about the live row;
  * raw user text can never reach the FTS5 parser as syntax (operators/colons in
    a query are literal, not injection), and a blank query returns nothing;
  * the REST endpoint requires an authenticated actor (a cross-cutting read over
    every issue and page) and can be narrowed to one kind.
"""
from fastapi.testclient import TestClient

from athena.aegis import issues, projects
from athena.core import db, search
from athena.main import create_app
from athena.mentor import pages, spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn):
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()


def _space(conn):
    return spaces.create_space(conn, key="ENG", name="Eng", created_by=1)


# --- unit: the index follows writes, across both kinds ----------------------


def test_create_issue_makes_it_findable(tmp_path):
    conn = _migrated_conn(tmp_path / "ci.db")
    _seed_user(conn)
    iss = issues.create_issue(
        conn, title="Build the board", body="kanban columns please", created_by=1
    )
    hits = search.search(conn, "kanban")
    assert [(h["kind"], h["source_id"]) for h in hits] == [("issue", iss["id"])]


def test_search_spans_issues_and_pages_in_one_query(tmp_path):
    # WHY: the headline payoff of one shared DB — a term that appears in both an
    # issue and a page returns BOTH in a single ranked result set, not two queries.
    conn = _migrated_conn(tmp_path / "span.db")
    _seed_user(conn)
    sp = _space(conn)
    iss = issues.create_issue(
        conn, title="Deploy pipeline", body="the release deploy steps", created_by=1
    )
    pg = pages.create_page(
        conn, space_id=sp["id"], title="Deploy runbook", body="how we deploy", created_by=1
    )
    hits = search.search(conn, "deploy")
    found = {(h["kind"], h["source_id"]) for h in hits}
    assert found == {("issue", iss["id"]), ("page", pg["id"])}


def test_title_hit_outranks_body_only_hit(tmp_path):
    # WHY: relevance — a doc whose TITLE matches the query is a stronger result than
    # one that only mentions it in passing in the body. bm25 weights title above
    # body, so the title match must come first.
    conn = _migrated_conn(tmp_path / "rank.db")
    _seed_user(conn)
    body_only = issues.create_issue(
        conn, title="random thing", body="we should migrate the database soon", created_by=1
    )
    title_hit = issues.create_issue(
        conn, title="Database migration plan", body="unrelated text", created_by=1
    )
    hits = search.search(conn, "database")
    assert [h["source_id"] for h in hits][0] == title_hit["id"]
    assert body_only["id"] in [h["source_id"] for h in hits]


def test_prefix_and_term_anding(tmp_path):
    # WHY: a search box must feel live — typing a partial word finds it, and adding
    # a second word narrows (AND), it does not widen.
    conn = _migrated_conn(tmp_path / "prefix.db")
    _seed_user(conn)
    a = issues.create_issue(conn, title="design the schema", body="", created_by=1)
    issues.create_issue(conn, title="schema only", body="", created_by=1)
    # Prefix: "desig" finds "design".
    assert [h["source_id"] for h in search.search(conn, "desig")] == [a["id"]]
    # AND: both terms must appear, so only the first issue qualifies.
    assert [h["source_id"] for h in search.search(conn, "design schema")] == [a["id"]]


def test_edit_reindexes_title_and_body(tmp_path):
    # WHY: the index is derived, never stale. Removing a word from the body stops it
    # matching; a NEW title becomes findable — both because the write path re-indexes.
    conn = _migrated_conn(tmp_path / "edit.db")
    _seed_user(conn)
    iss = issues.create_issue(
        conn, title="placeholder", body="contains zebra here", created_by=1
    )
    assert [h["source_id"] for h in search.search(conn, "zebra")] == [iss["id"]]
    issues.update_issue(conn, iss["id"], title="Aardvark report", body="no animals now")
    # Old body word is gone from the index...
    assert search.search(conn, "zebra") == []
    # ...and the new title is searchable.
    assert [h["source_id"] for h in search.search(conn, "aardvark")] == [iss["id"]]


def test_page_edit_reindexes(tmp_path):
    conn = _migrated_conn(tmp_path / "pgedit.db")
    _seed_user(conn)
    sp = _space(conn)
    pg = pages.create_page(
        conn, space_id=sp["id"], title="Notes", body="initial griffin content", created_by=1
    )
    assert [h["source_id"] for h in search.search(conn, "griffin")] == [pg["id"]]
    pages.update_page(conn, pg["id"], editor_id=1, body="rewritten phoenix content")
    assert search.search(conn, "griffin") == []
    assert [h["source_id"] for h in search.search(conn, "phoenix")] == [pg["id"]]


def test_kind_filter_narrows_to_one_module(tmp_path):
    conn = _migrated_conn(tmp_path / "kind.db")
    _seed_user(conn)
    sp = _space(conn)
    iss = issues.create_issue(conn, title="shared word alpha", body="", created_by=1)
    pg = pages.create_page(conn, space_id=sp["id"], title="shared word alpha", body="", created_by=1)
    assert [h["source_id"] for h in search.search(conn, "alpha", kind="issue")] == [iss["id"]]
    assert [h["source_id"] for h in search.search(conn, "alpha", kind="page")] == [pg["id"]]


def test_blank_query_returns_nothing(tmp_path):
    # WHY: an empty search box shows nothing, it does not dump the whole table.
    conn = _migrated_conn(tmp_path / "blank.db")
    _seed_user(conn)
    issues.create_issue(conn, title="something", body="", created_by=1)
    assert search.search(conn, "") == []
    assert search.search(conn, "   ") == []


def test_query_operators_are_treated_as_literal_text(tmp_path):
    # WHY: user input must never reach the FTS5 parser as syntax. A query full of
    # FTS5-special characters must not raise and must not act as operators — it is
    # tokenized and matched as plain text.
    conn = _migrated_conn(tmp_path / "inj.db")
    _seed_user(conn)
    iss = issues.create_issue(
        conn, title="set up the laptop", body="onboarding steps", created_by=1
    )
    # `set:up(` would be a syntax error / column-filter if passed raw; here it is
    # just the tokens "set" and "up".
    hits = search.search(conn, "set:up(")
    assert iss["id"] in [h["source_id"] for h in hits]
    # A lone FTS5 operator keyword is harmless too.
    assert search.search(conn, "AND OR NOT") == []


def test_snippet_highlights_the_match(tmp_path):
    conn = _migrated_conn(tmp_path / "snip.db")
    _seed_user(conn)
    issues.create_issue(
        conn, title="t", body="the quick brown fox jumps", created_by=1
    )
    hit = search.search(conn, "brown")[0]
    assert "[brown]" in hit["snippet"]


def test_backfill_makes_preexisting_rows_findable(tmp_path):
    # WHY: search must work for issues/pages that predate the feature. Migration
    # 0013 backfills the index from the existing tables. We simulate legacy data by
    # inserting straight into `issues` (bypassing the data layer that maintains the
    # index), so it is initially absent, then replay the backfill the migration runs
    # and confirm the old row becomes findable.
    conn = _migrated_conn(tmp_path / "bf.db")
    _seed_user(conn)
    conn.execute(
        "INSERT INTO issues (title, body, created_by) VALUES ('legacy', 'ancient runes', 1)"
    )
    conn.commit()
    assert search.search(conn, "runes") == []  # never went through index_document
    conn.execute("DELETE FROM search_index")
    conn.execute(
        "INSERT INTO search_index (kind, source_id, title, body) "
        "SELECT 'issue', id, title, body FROM issues"
    )
    conn.commit()
    assert [h["kind"] for h in search.search(conn, "runes")] == ["issue"]


# --- enrichment: hits carry the context that makes them scannable -----------


def test_issue_hit_carries_key_and_status(tmp_path):
    # WHY: a bare title is a weak result. An issue hit should carry its key (ATH-12)
    # and status so the searcher can triage from the results list without opening it.
    conn = _migrated_conn(tmp_path / "ctx.db")
    _seed_user(conn)
    proj = projects.create_project(conn, name="Web", key="WEB", created_by=1)
    iss = issues.create_issue(
        conn, title="zephyr handling", body="", created_by=1, project_id=proj["id"]
    )
    issues.update_issue(conn, iss["id"], status="done")
    hit = search.search(conn, "zephyr")[0]
    assert hit["key"] == "WEB-1"
    assert hit["status"] == "done"


def test_backlog_issue_hit_has_null_key(tmp_path):
    # WHY: an issue with no project has no key — the context resolves to None rather
    # than inventing one, and status is still present.
    conn = _migrated_conn(tmp_path / "blkey.db")
    _seed_user(conn)
    issues.create_issue(conn, title="orphan unicorn", body="", created_by=1)
    hit = search.search(conn, "unicorn")[0]
    assert hit["key"] is None
    assert hit["status"] == "open"


def test_page_hit_carries_space_key(tmp_path):
    # WHY: a page hit should say which space it lives in, so two same-named pages in
    # different spaces are distinguishable from the results list.
    conn = _migrated_conn(tmp_path / "pgctx.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="OPS", name="Ops", created_by=1)
    pages.create_page(conn, space_id=sp["id"], title="kraken runbook", body="", created_by=1)
    hit = search.search(conn, "kraken")[0]
    assert hit["space_key"] == "OPS"


def test_offset_pages_the_ranked_set(tmp_path):
    # WHY: a result set larger than one window must be walkable. limit+offset slice
    # the SAME ranked order, so page 2 continues where page 1 stopped — no repeats,
    # no gaps.
    conn = _migrated_conn(tmp_path / "page.db")
    _seed_user(conn)
    # Five issues all matching "lantern"; title hits, so a stable bm25 order.
    made = [
        issues.create_issue(conn, title=f"lantern {i}", body="", created_by=1)["id"]
        for i in range(5)
    ]
    first_two = [h["source_id"] for h in search.search(conn, "lantern", limit=2, offset=0)]
    next_two = [h["source_id"] for h in search.search(conn, "lantern", limit=2, offset=2)]
    assert len(first_two) == 2 and len(next_two) == 2
    # The two windows are disjoint and all come from the set we made.
    assert not set(first_two) & set(next_two)
    assert set(first_two + next_two) <= set(made)


# --- API: the REST endpoint -------------------------------------------------


def _seed_api_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    conn.close()


def test_search_endpoint_returns_ranked_hits_across_kinds(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "api.db")
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
        r = client.get("/search", params={"q": "telemetry"}, headers=h)
        assert r.status_code == 200
        found = {(x["kind"], x["source_id"]) for x in r.json()}
        assert found == {("issue", iss["id"]), ("page", pg["id"])}


def test_search_endpoint_kind_filter(tmp_path):
    app = create_app(tmp_path / "apik.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "apik.db")
        h = {"X-Athena-Actor": "1"}
        iss = client.post("/issues", json={"title": "widget alpha"}, headers=h).json()
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=h).json()
        client.post(f"/spaces/{sp['id']}/pages", json={"title": "widget alpha"}, headers=h)
        r = client.get("/search", params={"q": "widget", "kind": "issue"}, headers=h)
        assert [x["source_id"] for x in r.json()] == [iss["id"]]


def test_search_endpoint_requires_auth(tmp_path):
    # WHY: search exposes titles + body snippets from every issue and page. An
    # unauthenticated caller must not be able to enumerate the whole instance.
    app = create_app(tmp_path / "apinoauth.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "apinoauth.db")
        assert client.get("/search", params={"q": "anything"}).status_code == 401


def test_search_endpoint_missing_q_is_422(tmp_path):
    app = create_app(tmp_path / "apiq.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "apiq.db")
        h = {"X-Athena-Actor": "1"}
        assert client.get("/search", headers=h).status_code == 422


def test_search_endpoint_carries_context_and_pages(tmp_path):
    # WHY: the JSON surface exposes the same enrichment (issue key/status, page
    # space_key) and the same offset paging the web search uses, so an agent can
    # triage and walk results without a second round-trip per hit.
    app = create_app(tmp_path / "apictx.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "apictx.db")
        h = {"X-Athena-Actor": "1"}
        proj = client.post("/projects", json={"name": "Web", "key": "WEB"}, headers=h).json()
        client.post(
            "/issues",
            json={"title": "obelisk alpha", "project_id": proj["id"]},
            headers=h,
        )
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=h).json()
        client.post(f"/spaces/{sp['id']}/pages", json={"title": "obelisk beta"}, headers=h)
        hits = client.get("/search", params={"q": "obelisk"}, headers=h).json()
        by_kind = {x["kind"]: x for x in hits}
        assert by_kind["issue"]["key"] == "WEB-1"
        assert by_kind["issue"]["status"] == "open"
        assert by_kind["page"]["space_key"] == "ENG"
        # offset paging: limit 1 yields disjoint single-hit windows.
        p0 = client.get("/search", params={"q": "obelisk", "limit": 1, "offset": 0}, headers=h).json()
        p1 = client.get("/search", params={"q": "obelisk", "limit": 1, "offset": 1}, headers=h).json()
        assert len(p0) == 1 and len(p1) == 1
        assert p0[0]["source_id"] != p1[0]["source_id"] or p0[0]["kind"] != p1[0]["kind"]
