"""Outgoing links have a fixed read cost and retain each reader's view."""

from contextlib import closing

import pytest

from athena.aegis import issues, projects
from athena.core import access, db, links
from athena.mentor import pages, spaces


@pytest.fixture
def conn():
    with closing(db.connect(":memory:")) as connection:
        db.migrate(connection)
        connection.executemany(
            "INSERT INTO users (id, name, email, role) VALUES (?, ?, ?, ?)",
            [
                (
                    i,
                    name,
                    f"{name}@example.test",
                    "admin" if name == "admin" else "member",
                )
                for i, name in enumerate(
                    ("owner", "project-member", "space-member", "outsider", "admin"), 1
                )
            ],
        )
        connection.commit()
        yield connection


def edge(kind, target_id, title):
    return {"kind": kind, "id": target_id, "title": title, "exists": title is not None}


@pytest.mark.parametrize("size", [10, 100])
@pytest.mark.parametrize("source_kind", ["issue", "page"])
def test_each_size_reads_every_link_in_one_statement(conn, source_kind, size):
    space = spaces.create_space(conn, key="DOC", name="Docs", created_by=1, commit=True)
    project = projects.create_project(
        conn, key="WRK", name="Work", created_by=1, commit=True
    )
    issue_edges = []
    page_edges = []
    tokens = []
    for n in range(size // 2):
        issue = issues.create_issue(
            conn,
            project_id=project["id"],
            title=f"Task {n}",
            body="",
            created_by=1,
            commit=True,
        )
        page = pages.create_page(
            conn, space_id=space["id"], title=f"Doc {n}", created_by=1, commit=True
        )
        issue_edges.append(edge("issue", issue["id"], issue["title"]))
        page_edges.append(edge("page", page["id"], page["title"]))
        tokens.extend([f"[[issue:{issue['id']}]]", f"[[page:{page['id']}]]"])
    body = " ".join(reversed(tokens))
    if source_kind == "issue":
        source = issues.create_issue(
            conn, title="Source", body=body, created_by=1, commit=True
        )
    else:
        source = pages.create_page(
            conn,
            space_id=space["id"],
            title="Source",
            body=body,
            created_by=1,
            commit=True,
        )

    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = links.outgoing_links(
            conn, source_kind, source["id"], actor={"id": 4, "role": "member"}
        )
    finally:
        conn.set_trace_callback(None)

    assert result == issue_edges + page_edges
    assert len(statements) == 1


def test_current_targets_are_sorted_and_filtered_for_each_reader(conn):
    space = spaces.create_space(
        conn, key="PUB", name="Docs", created_by=1, commit=True
    )["id"]
    private_space = spaces.create_space(
        conn, key="PVT", name="Drafts", created_by=1, commit=True
    )["id"]
    project = projects.create_project(
        conn, key="WRK", name="Work", created_by=1, commit=True
    )["id"]
    spaces.set_visibility(conn, private_space, "private", commit=True)
    projects.set_visibility(conn, project, "private", commit=True)
    access.add_project_member(conn, project, user_id=2, added_by=1, commit=True)
    access.add_space_member(conn, private_space, user_id=3, added_by=1, commit=True)

    backlog = issues.create_issue(
        conn, title="Task draft", body="", created_by=1, commit=True
    )["id"]
    restricted = issues.create_issue(
        conn,
        project_id=project,
        title="Private task",
        body="",
        created_by=1,
        commit=True,
    )["id"]
    public = pages.create_page(
        conn, space_id=space, title="Doc draft", created_by=1, commit=True
    )["id"]
    draft = pages.create_page(
        conn, space_id=private_space, title="Private doc", created_by=1, commit=True
    )["id"]
    assert (backlog, restricted) == (public, draft)  # Distinct targets share IDs.
    source = pages.create_page(
        conn,
        space_id=space,
        title="Index",
        body=(
            f"[[page:{draft}]] [[issue:999]] [[page:{public}]] [[issue:{restricted}]] "
            f"[[issue:{backlog}]] [[page:999]] [[page:{public}]]"
        ),
        created_by=1,
        commit=True,
    )["id"]
    # The same ID under another source kind, and another ID under this source kind.
    decoy_body = f"[[page:{source}]]"
    assert (
        issues.create_issue(
            conn, title="Other", body=decoy_body, created_by=1, commit=True
        )["id"]
        == source
    )
    pages.create_page(
        conn, space_id=space, title="Other", body=decoy_body, created_by=1, commit=True
    )
    issues.update_issue(conn, backlog, title="Archived task", commit=True)
    pages.update_page(conn, public, editor_id=1, title="Archived doc", commit=True)
    issues.set_archived(conn, backlog, True, commit=True)
    pages.set_archived(conn, public, True, commit=True)

    task = edge("issue", backlog, "Archived task")
    private_task = edge("issue", restricted, "Private task")
    doc = edge("page", public, "Archived doc")
    private_doc = edge("page", draft, "Private doc")
    all_existing = [task, private_task, doc, private_doc]
    for actor, expected in (
        (None, [task, doc]),
        ({"id": 4, "role": "member"}, [task, doc]),
        ({"id": 2, "role": "member"}, [task, private_task, doc]),
        ({"id": 3, "role": "member"}, [task, doc, private_doc]),
        ({"id": 1, "role": "member"}, all_existing),
        ({"id": 5, "role": "admin"}, all_existing),
    ):
        assert links.outgoing_links(conn, "page", source, actor=actor) == expected, (
            actor
        )

    assert links.outgoing_links(conn, "page", source) == [
        task,
        private_task,
        edge("issue", 999, None),
        doc,
        private_doc,
        edge("page", 999, None),
    ]


def test_invalid_index_kind_is_reported(conn):
    conn.execute(
        "INSERT INTO links (source_kind, source_id, target_kind, target_id) "
        "VALUES ('page', 1, 'unknown', 1)"
    )
    with pytest.raises(KeyError, match="unknown"):
        links.outgoing_links(conn, "page", 1, actor=None)
