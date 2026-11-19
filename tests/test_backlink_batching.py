"""A target's backlinks resolve the current, visible sources in one read."""

from contextlib import closing

import pytest

from athena.aegis import issues, projects
from athena.core import access, db, links, users
from athena.mentor import pages, spaces


@pytest.fixture
def conn():
    with closing(db.connect(":memory:")) as conn:
        db.migrate(conn)
        for name in ("owner", "project-member", "space-member", "outsider", "admin"):
            users.create_user(
                conn,
                name=name,
                email=f"{name}@example.test",
                role="admin" if name == "admin" else "member",
            )
        yield conn


def record(kind, row_id, title):
    return {"kind": kind, "id": row_id, "title": title, "exists": title is not None}


def assert_backlinks(conn, kind, target_id, expected, **reader):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = links.backlinks(conn, kind, target_id, **reader)
    finally:
        conn.set_trace_callback(None)
    assert result == expected
    assert len(statements) == 1
    return statements[0]


@pytest.mark.parametrize("size", [10, 100])
@pytest.mark.parametrize("kind", ["page", "issue"])
def test_backlinks_read_all_sources_in_one_statement(conn, kind, size):
    space = spaces.create_space(conn, key="DOC", name="Docs", created_by=1)["id"]
    project = projects.create_project(conn, key="WRK", name="Work", created_by=1)["id"]
    issue = issues.create_issue(conn, title="Subject", body="", created_by=1)
    page = pages.create_page(conn, space_id=space, title="Subject", created_by=1)
    target_id = issue["id"]
    assert page["id"] == target_id
    other_kind = "page" if kind == "issue" else "issue"
    # Neither an edge to a different kind at the same ID nor to another ID counts.
    decoy = issues.create_issue(
        conn,
        title="Decoy",
        created_by=1,
        body=f"[[{other_kind}:{target_id}]] [[{kind}:9999]]",
    )
    # Both subjects reference the decoy, which only cites the other subject.
    outbound = f"[[issue:{decoy['id']}]]"
    issues.update_issue(conn, target_id, body=outbound)
    pages.update_page(conn, target_id, body=outbound, editor_id=1)
    reader = {"id": 4, "role": "member"}
    assert_backlinks(conn, kind, target_id, [], actor=reader)
    issue_refs, page_refs = [], []
    for n in range(size // 2, 0, -1):
        body = f"[[{kind}:{target_id}]]"
        page = pages.create_page(
            conn, space_id=space, title=f"Page {n}", body=body, created_by=1
        )
        issue = issues.create_issue(
            conn, project_id=project, title=f"Issue {n}", body=body, created_by=1
        )
        page_refs.append(record("page", page["id"], page["title"]))
        issue_refs.append(record("issue", issue["id"], issue["title"]))
    sql = assert_backlinks(conn, kind, target_id, issue_refs + page_refs, actor=reader)
    assert links.outgoing_links(conn, kind, target_id, actor=reader) == [
        record("issue", decoy["id"], decoy["title"])
    ]
    plan = conn.execute("EXPLAIN QUERY PLAN " + sql)
    assert any(
        "SEARCH" in row["detail"] and "idx_links_target" in row["detail"]
        for row in plan
    )


def test_backlinks_follow_current_source_visibility_and_titles(conn):
    space = spaces.create_space(conn, key="PUB", name="Public", created_by=1)["id"]
    private_space = spaces.create_space(conn, key="PVT", name="Private", created_by=1)[
        "id"
    ]
    project = projects.create_project(conn, key="WRK", name="Private", created_by=1)[
        "id"
    ]
    spaces.set_visibility(conn, private_space, "private")
    projects.set_visibility(conn, project, "private")
    access.add_project_member(conn, project, user_id=2, added_by=1)
    access.add_space_member(conn, private_space, user_id=3, added_by=1)

    target_issue = issues.create_issue(conn, title="Target", body="", created_by=1)[
        "id"
    ]
    target_page = pages.create_page(conn, space_id=space, title="Target", created_by=1)[
        "id"
    ]
    body = f"[[issue:{target_issue}]] [[page:{target_page}]]"
    backlog = issues.create_issue(conn, title="Draft task", body=body, created_by=4)[
        "id"
    ]
    restricted = issues.create_issue(
        conn, project_id=project, title="Private task", body=body, created_by=4
    )["id"]
    public = pages.create_page(
        conn, space_id=space, title="Draft doc", body=body, created_by=4
    )["id"]
    draft = pages.create_page(
        conn, space_id=private_space, title="Private doc", body=body, created_by=4
    )["id"]
    assert (backlog, restricted) == (public, draft)
    issues.update_issue(conn, backlog, title="Archived task")
    pages.update_page(conn, public, title="Archived doc", editor_id=1)
    issues.set_archived(conn, backlog, True)
    pages.set_archived(conn, public, True)
    targets = [("issue", target_issue), ("page", target_page)]
    conn.executemany(
        "INSERT INTO links (source_kind, source_id, target_kind, target_id) VALUES ('issue', 999, ?, ?)",
        targets,
    )

    task = record("issue", backlog, "Archived task")
    private_task = record("issue", restricted, "Private task")
    doc = record("page", public, "Archived doc")
    private_doc = record("page", draft, "Private doc")
    all_existing = [task, private_task, doc, private_doc]
    public_only = [task, doc]
    project_member = {"id": 2, "role": "member"}
    space_member = {"id": 3, "role": "member"}
    for kind, target_id in targets:
        for reader, expected in (
            (None, public_only),
            ({"id": 4, "role": "member"}, public_only),
            (project_member, [task, private_task, doc]),
            (space_member, [task, doc, private_doc]),
            ({"id": 1, "role": "member"}, all_existing),
            ({"id": 5, "role": "admin"}, all_existing),
        ):
            assert_backlinks(conn, kind, target_id, expected, actor=reader)
        assert_backlinks(
            conn,
            kind,
            target_id,
            [task, private_task, record("issue", 999, None), doc, private_doc],
        )

    access.remove_project_member(conn, project, user_id=2)
    access.remove_space_member(conn, private_space, user_id=3)
    for kind, target_id in targets:
        for reader in (project_member, space_member):
            assert_backlinks(conn, kind, target_id, public_only, actor=reader)

    issues.set_project(conn, backlog, project)
    issues.set_project(conn, restricted, None)
    access.add_project_member(conn, project, user_id=2, added_by=1)
    for kind, target_id in targets:
        assert_backlinks(conn, kind, target_id, [private_task, doc], actor=None)
        assert_backlinks(
            conn, kind, target_id, [task, private_task, doc], actor=project_member
        )


def test_unknown_backlink_source_kind_remains_an_index_error(conn):
    conn.execute("INSERT INTO links VALUES ('unknown', 1, 'issue', 1)")
    with pytest.raises(KeyError, match="unknown"):
        links.backlinks(conn, "issue", 1, actor=None)
