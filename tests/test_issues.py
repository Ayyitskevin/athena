"""Tests for the Aegis issues API: create, list, and the boundary rules.

Issues are created *by* someone. The creator is the actor on the request
(the `X-Athena-Actor` header), not a value the caller puts in the body — so
these tests drive creation through that header.
"""
import pytest
from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import db
from athena.main import create_app


def _seed_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", ("kevin@example.com", "Kevin"))
    conn.commit()
    conn.close()


def test_create_then_list_issue(tmp_path):
    db_file = tmp_path / "issues.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)

        created = client.post(
            "/issues", json={"title": "ship it"}, headers={"X-Athena-Actor": "1"}
        )
        assert created.status_code == 201
        body = created.json()
        assert body["title"] == "ship it"
        assert body["status"] == "open"  # the schema DEFAULT applied
        assert body["created_by"] == 1  # stamped from the actor, not the request body
        assert body["id"] == 1

        listing = client.get("/issues")
        assert listing.status_code == 200
        assert [i["title"] for i in listing.json()] == ["ship it"]


def test_missing_actor_header_is_rejected(tmp_path):
    # WHY: an issue must be attributed to someone. No actor, no write.
    db_file = tmp_path / "noactor.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post("/issues", json={"title": "anonymous"})
        assert r.status_code == 401


def test_unknown_actor_is_rejected(tmp_path):
    # WHY: the actor header identifies a real user — a made-up id is not a free
    # pass to write. (It identifies, it doesn't yet authenticate; see identity.py.)
    app = create_app(tmp_path / "bad.db")
    with TestClient(app) as client:
        r = client.post("/issues", json={"title": "orphan"}, headers={"X-Athena-Actor": "999"})
        assert r.status_code == 401


def test_missing_title_is_a_validation_error(tmp_path):
    # WHY: Pydantic rejects malformed input (422) before the handler runs. A
    # valid actor isolates the failure to the body.
    db_file = tmp_path / "v.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post("/issues", json={}, headers={"X-Athena-Actor": "1"})
        assert r.status_code == 422


def test_blank_title_is_422(tmp_path):
    # WHY: a present-but-whitespace title is still nameless. The API must reject
    # it (422) like the web form does, so the two surfaces can't disagree — Codex
    # found POST /issues with "   " was returning 201.
    db_file = tmp_path / "blank.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post(
            "/issues", json={"title": "   "}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 422


def test_title_is_persisted_stripped(tmp_path):
    # WHY: surrounding whitespace is noise, not part of the title. What we store
    # (and echo back) is the trimmed value.
    db_file = tmp_path / "strip.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.post(
            "/issues", json={"title": "  spaced out  "}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 201
        assert r.json()["title"] == "spaced out"


def test_get_single_issue(tmp_path):
    # WHY: API-first means every issue is addressable on its own URL, not only
    # as a row in the list. GET /issues/{id} is the canonical single read.
    db_file = tmp_path / "one.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        created = client.post(
            "/issues", json={"title": "find me"}, headers={"X-Athena-Actor": "1"}
        ).json()
        r = client.get(f"/issues/{created['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == created["id"]
        assert body["title"] == "find me"
        assert body["labels"] == []  # composed in like every other issue read


def test_get_single_issue_is_open_to_anyone(tmp_path):
    # WHY: reads are open — no actor header required, same contract as the list
    # endpoint. Only writes pass through the creator-or-assignee gate.
    db_file = tmp_path / "open.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        created = client.post(
            "/issues", json={"title": "public read"}, headers={"X-Athena-Actor": "1"}
        ).json()
        r = client.get(f"/issues/{created['id']}")  # no actor header
        assert r.status_code == 200


def test_get_missing_issue_is_404(tmp_path):
    db_file = tmp_path / "missing.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        r = client.get("/issues/999")
        assert r.status_code == 404


# --- list filtering via query params --------------------------------------
#
# These let a programmatic actor (the AI fleet) query the issue list the same
# way the web UI filters it — one shared path in issues.list_issues, so the API
# and the browser never disagree on what matches.


def _mk(client, title, **body):
    return client.post(
        "/issues", json={"title": title, **body}, headers={"X-Athena-Actor": "1"}
    ).json()


def test_list_filters_by_status(tmp_path):
    db_file = tmp_path / "fs.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _mk(client, "open one")
        _mk(client, "done one", status="done")
        titles = [i["title"] for i in client.get("/issues?status=done").json()]
        assert titles == ["done one"]


def test_list_search_matches_title_or_body_case_insensitively(tmp_path):
    # WHY: search is a substring on title OR body, case-insensitive — the same
    # contract the web search box has.
    db_file = tmp_path / "se.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _mk(client, "Login bug", body="crashes on submit")
        _mk(client, "Dashboard", body="add a Widget")
        _mk(client, "Unrelated", body="nothing here")
        by_title = {i["title"] for i in client.get("/issues?search=login").json()}
        assert by_title == {"Login bug"}
        by_body = {i["title"] for i in client.get("/issues?search=WIDGET").json()}
        assert by_body == {"Dashboard"}


def test_list_filters_by_label(tmp_path):
    # WHY: label filtering is resolved through labels.py (name -> issue ids) so
    # issues.py never imports labels; the API still filters by it end to end.
    db_file = tmp_path / "fl.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        bug = _mk(client, "has bug label")
        _mk(client, "no label")
        lab = client.post(
            "/labels", json={"name": "bug"}, headers={"X-Athena-Actor": "1"}
        ).json()
        client.post(
            f"/issues/{bug['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        titles = [i["title"] for i in client.get("/issues?label=bug").json()]
        assert titles == ["has bug label"]


def test_list_unknown_label_matches_nothing(tmp_path):
    # WHY: an empty id set must yield no rows (and never a malformed "IN ()").
    db_file = tmp_path / "ul.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _mk(client, "something")
        assert client.get("/issues?label=ghost").json() == []


def test_list_filters_compose(tmp_path):
    # WHY: status + search + label must AND together — narrowing, not replacing.
    db_file = tmp_path / "compose.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        target = _mk(client, "urgent login fix", status="done")
        _mk(client, "login fix", status="open")  # right text, wrong status
        lab = client.post(
            "/labels", json={"name": "auth"}, headers={"X-Athena-Actor": "1"}
        ).json()
        client.post(
            f"/issues/{target['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        titles = [
            i["title"]
            for i in client.get("/issues?status=done&search=login&label=auth").json()
        ]
        assert titles == ["urgent login fix"]


def test_list_no_filters_returns_all(tmp_path):
    db_file = tmp_path / "all.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        _mk(client, "one")
        _mk(client, "two")
        assert len(client.get("/issues").json()) == 2


def test_list_is_paginated_and_bounded(tmp_path):
    # WHY: GET /issues is anon-reachable, so an unbounded list is a credential-free
    # DoS vector (pull the whole table in one request) — the same gap A5 closed on
    # /issues/search. The page is capped (≤ 100, default 50) and offset walks it, over
    # a stable id order; out-of-range limits are rejected at the boundary (422).
    db_file = tmp_path / "page.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        ids = [_mk(client, f"i{n}")["id"] for n in range(5)]

        first_two = client.get("/issues?limit=2")
        assert first_two.status_code == 200
        assert [i["id"] for i in first_two.json()] == ids[:2]
        # offset walks the pages, same stable order.
        assert [i["id"] for i in client.get("/issues?limit=2&offset=2").json()] == ids[2:4]
        assert [i["id"] for i in client.get("/issues?limit=2&offset=4").json()] == ids[4:]

        # The cap is enforced at the boundary — no unbounded/garbage page sizes.
        assert client.get("/issues?limit=0").status_code == 422
        assert client.get("/issues?limit=-1").status_code == 422
        assert client.get("/issues?limit=101").status_code == 422
        assert client.get("/issues?offset=-1").status_code == 422
        assert client.get(f"/issues?offset={issues.MAX_OFFSET}").status_code == 200
        assert (
            client.get(f"/issues?offset={issues.MAX_OFFSET + 1}").status_code
            == 422
        )


def test_list_data_layer_rejects_unbindable_offset(tmp_path):
    # WHY: list_issues is shared beyond HTTP. Its direct callers should get a
    # deterministic validation error before sqlite3 tries to bind an oversized int.
    conn = db.connect(tmp_path / "data-offset.db")
    db.migrate(conn)
    try:
        with pytest.raises(ValueError, match="offset must be between"):
            issues.list_issues(conn, limit=1, offset=issues.MAX_OFFSET + 1)
    finally:
        conn.close()


def test_list_default_page_size_is_bounded(tmp_path):
    # WHY: even with no limit param, the response is capped at the default page (50),
    # not the whole table — the point of the fix. 55 issues, default GET returns 50.
    db_file = tmp_path / "default_cap.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        for n in range(55):
            _mk(client, f"n{n}")
        assert len(client.get("/issues").json()) == 50
        # The tail is reachable by paging past the first page.
        assert len(client.get("/issues?offset=50").json()) == 5
