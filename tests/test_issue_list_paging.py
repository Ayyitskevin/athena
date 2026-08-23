"""The issue list and the board page in SQL, and rank priority the way the grammar does.

Both surfaces used to fetch EVERY matching issue, attach every issue's labels, sort the
whole list in Python and then slice a page out of it — and the status dropdown above
them ran a second unbounded fetch just to collect distinct status names. That cost grew
with the tracker: measured 74 ms per issue-list page view at 10k issues against 0.2 ms
for the bounded read the REST endpoint was already doing.

These tests hold the fix to the two things that could go wrong when a read becomes
bounded: the page must contain the rows it would have contained (including the SORT,
which is only correct if it happens before the slice — sorting after LIMIT reorders
whatever reached the page), and the work must actually be bounded rather than merely
moved.
"""

from __future__ import annotations


from fastapi.testclient import TestClient

from athena.aegis import issue_query, issues, projects
from athena.core import db, users, work_query
from athena.main import create_app


def _fixture(tmp_path, *, count=60):
    conn = db.connect(tmp_path / "issues.db")
    db.migrate(conn)
    admin = users.create_user(conn, email="a@e.com", name="Admin", role="admin")
    owner = users.create_user(conn, email="o@e.com", name="Owner")
    outsider = users.create_user(conn, email="x@e.com", name="Outsider")
    pub = projects.create_project(conn, name="Pub", key="PUB", created_by=owner["id"])
    priv = projects.create_project(conn, name="Priv", key="PRV", created_by=owner["id"])
    conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (priv["id"],))
    conn.commit()
    priorities = ("low", "medium", "high", "urgent")
    for n in range(count):
        issues.create_issue(
            conn,
            title=f"Issue {n:03d}",
            body="body",
            created_by=owner["id"],
            status=("open", "in_progress", "done")[n % 3],
            priority=priorities[n % 4],
            project_id=(pub, priv, None)[n % 3]["id"]
            if (pub, priv, None)[n % 3]
            else None,
        )
    return conn, {
        "admin": admin,
        "owner": owner,
        "out": outsider,
        "pub": pub,
        "priv": priv,
    }


def test_paged_read_equals_the_unbounded_read_sliced(tmp_path):
    """A page is the same rows the old fetch-everything-then-slice produced, for every
    sort the web list offers, in both directions."""
    conn, _who = _fixture(tmp_path)
    for sort in issues.LIST_SORTS:
        for order in ("asc", "desc"):
            whole = issues.list_issues(conn, sort=sort, order=order)
            for page, per_page in ((1, 20), (2, 20), (3, 20), (1, 7), (4, 7)):
                offset = (page - 1) * per_page
                paged = issues.list_issues(
                    conn, sort=sort, order=order, limit=per_page, offset=offset
                )
                assert [i["id"] for i in paged] == [
                    i["id"] for i in whole[offset : offset + per_page]
                ], (sort, order, page)


def test_every_sort_is_total_so_pages_cannot_overlap_or_drop_rows(tmp_path):
    """A non-total ordering lets SQLite return a row on two pages (or none). Walk every
    sort in 7-row pages and require the walk to reconstruct the full list exactly."""
    conn, _who = _fixture(tmp_path)
    for sort in issues.LIST_SORTS:
        for order in ("asc", "desc"):
            expected = [
                i["id"] for i in issues.list_issues(conn, sort=sort, order=order)
            ]
            walked: list[int] = []
            offset = 0
            while True:
                page = issues.list_issues(
                    conn, sort=sort, order=order, limit=7, offset=offset
                )
                if not page:
                    break
                walked.extend(i["id"] for i in page)
                offset += 7
            assert walked == expected, (sort, order)
            assert len(walked) == len(set(walked)), (sort, order)


def test_priority_sorts_by_rank_not_alphabetically(tmp_path):
    """The bug this shares away: sorting priority as text reads urgent, medium, low,
    high. Descending priority means MOST URGENT FIRST."""
    conn, _who = _fixture(tmp_path)
    ordered = issues.list_issues(conn, sort="priority", order="desc")
    seen = [i["priority"] for i in ordered]
    assert seen[0] == "urgent" and seen[-1] == "low"
    # Ranks never interleave: all of one priority, then all of the next.
    rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    assert seen == sorted(seen, key=lambda p: rank[p])


def test_web_priority_sort_and_grammar_priority_sort_are_one_ordering(tmp_path):
    """The acceptance criterion for the drift: `sort:priority-desc` in a work query and
    the web list's priority sort must return the SAME ordering, not two orderings that
    happen to agree on the first column."""
    conn, _who = _fixture(tmp_path)
    for direction, grammar_key in (("desc", "priority-desc"), ("asc", "priority-asc")):
        from_list = [
            i["id"] for i in issues.list_issues(conn, sort="priority", order=direction)
        ]
        from_grammar = [
            i["id"]
            for i in issue_query.run_query(
                conn,
                work_query.parse(f"sort:{grammar_key}"),
                actor=None,
                visible_project_ids=None,
                limit=10_000,
            )
        ]
        assert from_list == from_grammar, direction


def test_count_agrees_with_the_list_it_labels(tmp_path):
    """The total renders "N issues" above the page; it must describe the same query."""
    conn, who = _fixture(tmp_path)
    filter_sets: list[dict] = [
        {},
        {"status": "open"},
        {"priority": "urgent"},
        {"search": "Issue 01"},
        {"project_id": who["pub"]["id"]},
        {"backlog": True},
        {"include_archived": True},
        {"visible_project_ids": {who["pub"]["id"]}},
        {"visible_project_ids": set()},
        {"ids": []},
        {"assignee_id": 10**19},  # unusable id: matches nothing, never everything
    ]
    for filters in filter_sets:
        assert issues.count_issues(conn, **filters) == len(
            issues.list_issues(conn, **filters)
        ), filters


def test_statuses_in_use_is_gated_and_bounded(tmp_path):
    """The dropdown must not leak a private project's custom status name, and must not
    read the whole table to build itself."""
    conn, who = _fixture(tmp_path)
    conn.execute(
        "UPDATE issues SET status='secret_status' WHERE project_id=?",
        (who["priv"]["id"],),
    )
    conn.commit()

    assert "secret_status" in issues.statuses_in_use(conn)  # ungated (admin view)
    visible_to_outsider = issues.statuses_in_use(
        conn, visible_project_ids={who["pub"]["id"]}
    )
    assert "secret_status" not in visible_to_outsider

    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    issues.statuses_in_use(conn, visible_project_ids={who["pub"]["id"]})
    conn.set_trace_callback(None)
    assert len(traced) == 1, traced
    assert "DISTINCT" in traced[0].upper()


def test_issue_list_page_view_does_not_hydrate_every_issue(tmp_path):
    """The regression that matters: rendering one page must not read every matching
    row. Pinned by counting the rows the handler's issue read actually returns."""
    app = create_app(str(tmp_path / "web.db"))
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "Admin", "password": "pw"},
            headers={"X-Athena-Actor": "1"},
        )
        for n in range(45):
            client.post(
                "/issues",
                json={"title": f"Issue {n:03d}", "body": "b"},
                headers={"X-Athena-Actor": "1"},
            )
        response = client.get(
            "/aegis/issues?per_page=20&page=1", headers={"X-Athena-Actor": "1"}
        )
        assert response.status_code == 200
        assert "45" in response.text  # the total still describes every match

        sizes: list[int] = []
        original = issues.list_issues

        def counting_list_issues(*args, **kwargs):
            rows = original(*args, **kwargs)
            sizes.append(len(rows))
            return rows

        # The handler resolves `issues.list_issues` on the module object at call time,
        # so patching the attribute here is what it will reach.
        issues.list_issues = counting_list_issues  # type: ignore[assignment]
        try:
            client.get(
                "/aegis/issues?per_page=20&page=1", headers={"X-Athena-Actor": "1"}
            )
        finally:
            issues.list_issues = original  # type: ignore[assignment]
        assert sizes, "the handler no longer reads issues at all"
        assert max(sizes) <= 20, sizes


def test_board_caps_its_cards_and_says_so(tmp_path, monkeypatch):
    """A board has no next page, so a clipped board must disclose the clip rather than
    presenting a prefix as the whole picture."""
    from athena.web import boards

    monkeypatch.setattr(boards, "BOARD_CARD_CAP", 5)
    app = create_app(str(tmp_path / "board.db"))
    with TestClient(app) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "Admin", "password": "pw"},
            headers={"X-Athena-Actor": "1"},
        )
        for n in range(12):
            client.post(
                "/issues",
                json={"title": f"Card {n}", "body": "b"},
                headers={"X-Athena-Actor": "1"},
            )
        page = client.get("/aegis/boards", headers={"X-Athena-Actor": "1"})
        assert page.status_code == 200
        assert "Showing 5 of 12" in page.text

    app2 = create_app(str(tmp_path / "board2.db"))
    with TestClient(app2) as client:
        client.post(
            "/users",
            json={"email": "a@e.com", "name": "Admin", "password": "pw"},
            headers={"X-Athena-Actor": "1"},
        )
        for n in range(3):
            client.post(
                "/issues",
                json={"title": f"Card {n}", "body": "b"},
                headers={"X-Athena-Actor": "1"},
            )
        page = client.get("/aegis/boards", headers={"X-Athena-Actor": "1"})
        assert "Showing" not in page.text  # an uncapped board says nothing
