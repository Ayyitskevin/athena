"""Saved-filter assignee ids stay strict, bounded, and fail closed.

An assignee filter is optional, but once supplied it must never be silently
dropped: that turns a narrow query into an all-assignees query. It also has to
fit SQLite's signed-integer bind range so a stored query cannot become a later
500 when it runs.
"""

import json

import pytest
from fastapi.testclient import TestClient

from athena.aegis import issues, saved_filters
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _admin(client):
    client.post(
        "/users",
        json={"email": "a@e.com", "name": "Alice", "password": "pw"},
    )


def _login(client):
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, {}),
        ("", {}),
        ("   ", {}),
        (0, {"assignee_id": 0}),
        ("0", {"assignee_id": 0}),
        (" 007 ", {"assignee_id": 7}),
        (issues.MAX_SQLITE_INTEGER, {"assignee_id": issues.MAX_SQLITE_INTEGER}),
        (str(issues.MAX_SQLITE_INTEGER), {"assignee_id": issues.MAX_SQLITE_INTEGER}),
    ],
)
def test_normalize_assignee_accepts_only_omitted_or_bounded_ids(value, expected):
    assert saved_filters.normalize_criteria({"assignee_id": value}) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.0,
        1.5,
        -1,
        issues.MAX_SQLITE_INTEGER + 1,
        "+1",
        "-1",
        "1.0",
        "1e3",
        "abc",
        "١",
        [],
        {},
        "9" * 101,
    ],
)
def test_normalize_assignee_rejects_values_that_would_coerce_or_widen(value):
    with pytest.raises(
        saved_filters.InvalidFilterCriteria, match="invalid assignee filter"
    ):
        saved_filters.normalize_criteria({"assignee_id": value})


def test_direct_filter_writes_reject_invalid_criteria_before_sql(tmp_path):
    # WHY: HTTP adapters validate too, but the storage boundary must remain safe for
    # future internal callers. An invalid update must not partially apply its rename.
    conn = db.connect(tmp_path / "direct-writes.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'Alice')")
    conn.commit()
    try:
        with pytest.raises(saved_filters.InvalidFilterCriteria):
            saved_filters.create_filter(
                conn,
                owner_id=1,
                name="overflow",
                criteria={"assignee_id": issues.MAX_SQLITE_INTEGER + 1},
            )
        with pytest.raises(saved_filters.InvalidFilterCriteria):
            saved_filters.create_filter(
                conn,
                owner_id=1,
                name="unknown",
                criteria={"sprint_id": 1},
            )
        assert saved_filters.list_filters(conn, 1) == []

        created = saved_filters.create_filter(
            conn, owner_id=1, name="stable", criteria={"assignee_id": 0}
        )
        with pytest.raises(saved_filters.InvalidFilterCriteria):
            saved_filters.update_filter(
                conn,
                created["id"],
                owner_id=1,
                name="must-not-apply",
                criteria={"assignee_id": True},
            )
        unchanged = saved_filters.get_filter(conn, created["id"])
        assert unchanged["name"] == "stable"
        assert unchanged["criteria"] == {"assignee_id": 0}
    finally:
        conn.close()


def test_api_rejects_non_integer_and_unbindable_assignees_without_persisting(tmp_path):
    with TestClient(create_app(tmp_path / "api-invalid.db")) as client:
        _admin(client)
        invalid_values = [
            True,
            False,
            1.0,
            1.5,
            "1",
            "01",
            -1,
            issues.MAX_SQLITE_INTEGER + 1,
        ]
        for index, value in enumerate(invalid_values):
            response = client.post(
                "/filters",
                json={"name": f"bad-{index}", "criteria": {"assignee_id": value}},
                headers=H1,
            )
            assert response.status_code == 422, (value, response.text)
        response = client.post(
            "/filters",
            json={"name": "unknown", "criteria": {"sprint_id": 1}},
            headers=H1,
        )
        assert response.status_code == 422
        assert "extra_forbidden" in response.text

        assert client.get("/filters", headers=H1).json() == []


def test_api_patch_rejection_leaves_the_saved_filter_unchanged(tmp_path):
    with TestClient(create_app(tmp_path / "api-patch.db")) as client:
        _admin(client)
        created = client.post(
            "/filters",
            json={"name": "stable", "criteria": {"assignee_id": 0}},
            headers=H1,
        ).json()

        response = client.patch(
            f"/filters/{created['id']}",
            json={
                "name": "must-not-apply",
                "criteria": {"assignee_id": issues.MAX_SQLITE_INTEGER + 1},
            },
            headers=H1,
        )
        assert response.status_code == 422
        assert client.get(f"/filters/{created['id']}", headers=H1).json() == created


def test_api_accepts_bindable_unknown_ids_and_runs_them_as_no_match(tmp_path):
    with TestClient(create_app(tmp_path / "api-valid.db")) as client:
        _admin(client)
        for index, value in enumerate((0, 999_999, issues.MAX_SQLITE_INTEGER)):
            created = client.post(
                "/filters",
                json={"name": f"valid-{index}", "criteria": {"assignee_id": value}},
                headers=H1,
            )
            assert created.status_code == 201
            assert created.json()["criteria"] == {"assignee_id": value}
            run = client.get(f"/filters/{created.json()['id']}/issues", headers=H1)
            assert run.status_code == 200
            assert run.json() == []


def test_web_rejects_malformed_assignees_without_creating_a_wide_filter(tmp_path):
    with TestClient(create_app(tmp_path / "web-invalid.db")) as client:
        _admin(client)
        csrf = _login(client)
        for index, value in enumerate(
            ("-1", "+1", "1.0", "1e3", "abc", "١", str(issues.MAX_SQLITE_INTEGER + 1))
        ):
            response = client.post(
                "/aegis/filters",
                data={"name": f"bad-{index}", "assignee_id": value},
                headers={"X-CSRF-Token": csrf},
                follow_redirects=False,
            )
            assert response.status_code == 400, (value, response.text)
        assert client.get("/filters", headers=H1).json() == []


def test_web_canonicalizes_ascii_decimal_assignee_ids(tmp_path):
    with TestClient(create_app(tmp_path / "web-valid.db")) as client:
        _admin(client)
        csrf = _login(client)
        for index, (raw, expected) in enumerate(
            (
                ("0", 0),
                ("01", 1),
                (str(issues.MAX_SQLITE_INTEGER), issues.MAX_SQLITE_INTEGER),
            )
        ):
            response = client.post(
                "/aegis/filters",
                data={"name": f"valid-{index}", "assignee_id": raw},
                headers={"X-CSRF-Token": csrf},
                follow_redirects=False,
            )
            assert response.status_code == 303
        stored = client.get("/filters", headers=H1).json()
        assert [flt["criteria"]["assignee_id"] for flt in stored] == [
            0,
            1,
            issues.MAX_SQLITE_INTEGER,
        ]


def test_legacy_invalid_rows_remain_readable_and_fail_closed(tmp_path):
    # WHY: older Athena versions could persist an oversized integer that later
    # raised OverflowError. Nonnumeric/project rows plus malformed, non-object, and
    # unknown-key JSON model imported or manually corrupted state. Plain run and
    # search-within must agree on no match; corrupt JSON must never decode as the
    # valid empty "all issues" filter.
    db_file = tmp_path / "legacy.db"
    with TestClient(create_app(db_file)) as client:
        _admin(client)
        _login(client)
        issue = client.post(
            "/issues", json={"title": "legacy canonicalization target"}, headers=H1
        ).json()
        client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": 1},
            headers=H1,
        )

        conn = db.connect(db_file)
        try:
            filter_ids = []
            legacy_payloads = (
                json.dumps({"assignee_id": issues.MAX_SQLITE_INTEGER + 1}),
                json.dumps({"assignee_id": "abc"}),
                json.dumps({"project": "bogus"}),
                "{",
                json.dumps([{"assignee_id": 1}]),
                json.dumps(None),
                json.dumps(1),
                json.dumps("legacy scalar"),
                json.dumps({"assigneee_id": 1}),
            )
            for index, payload in enumerate(legacy_payloads):
                cur = conn.execute(
                    "INSERT INTO saved_filters (owner_id, name, criteria) VALUES (?, ?, ?)",
                    (1, f"legacy-{index}", payload),
                )
                filter_ids.append(cur.lastrowid)
            canonical = conn.execute(
                "INSERT INTO saved_filters (owner_id, name, criteria) VALUES (?, ?, ?)",
                (1, "legacy-numeric-string", json.dumps({"assignee_id": "1"})),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        listing = client.get("/aegis/filters")
        assert listing.status_code == 200
        assert listing.text.count("invalid filter") == len(filter_ids)

        api_listing = client.get("/filters", headers=H1)
        assert api_listing.status_code == 200
        by_name = {row["name"]: row for row in api_listing.json()}
        assert by_name["legacy-3"]["criteria"] is None
        assert by_name["legacy-4"]["criteria"] is None

        for filter_id in filter_ids:
            run = client.get(f"/filters/{filter_id}/issues", headers=H1)
            assert run.status_code == 200
            assert run.json() == []

            plain = client.get(f"/aegis/filters/{filter_id}")
            assert plain.status_code == 200
            assert issue["title"] not in plain.text

            searched = client.get(f"/aegis/filters/{filter_id}?q=target")
            assert searched.status_code == 200
            assert issue["title"] not in searched.text

        run = client.get(f"/filters/{canonical}/issues", headers=H1)
        assert [row["id"] for row in run.json()] == [issue["id"]]
        assert issue["title"] in client.get(f"/aegis/filters/{canonical}").text
        assert issue["title"] in client.get(f"/aegis/filters/{canonical}?q=target").text


def test_list_issues_rejects_invalid_direct_filter_ids_without_sqlite_coercion(
    tmp_path,
):
    conn = db.connect(tmp_path / "direct-query.db")
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'Alice')")
    conn.commit()
    issues.create_issue(conn, title="must not leak", body="", created_by=1)
    try:
        assert len(issues.list_issues(conn)) == 1
        invalid_values = (
            True,
            False,
            -1,
            -(1 << 63) - 1,
            issues.MAX_SQLITE_INTEGER + 1,
            1.0,
            "1",
        )
        for field_name in ("assignee_id", "project_id", "sprint_id"):
            for value in invalid_values:
                assert issues.list_issues(conn, **{field_name: value}) == []
            for value in (0, issues.MAX_SQLITE_INTEGER):
                assert issues.list_issues(conn, **{field_name: value}) == []
    finally:
        conn.close()


def test_ranked_issue_search_rejects_unbindable_assignee_at_http_boundary(tmp_path):
    # WHY: /issues and /issues/search share assignee semantics. Both should reject
    # an unbindable id as caller error, while exact SQLite bounds remain safe.
    with TestClient(create_app(tmp_path / "search-boundary.db")) as client:
        _admin(client)
        assert (
            client.get(
                "/issues/search",
                params={"q": "nothing", "assignee": issues.MAX_SQLITE_INTEGER},
            ).status_code
            == 200
        )
        for invalid in (-1, issues.MAX_SQLITE_INTEGER + 1):
            assert (
                client.get(
                    "/issues/search",
                    params={"q": "nothing", "assignee": invalid},
                ).status_code
                == 422
            )
