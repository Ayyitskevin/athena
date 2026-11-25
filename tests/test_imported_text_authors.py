"""Imported comments and pages say Athena does not know who wrote them (G3).

WHY: an import maps each bundle account onto the local account with the same
email and writes comments, pages and page versions under that account's id.
``authored_by`` (A10) read that column, so an agent was told the local account
wrote text Athena was only given. Imported events answered ``null`` since 0041;
these rows carried no mark. Migration 0094 marks them (``imported_at``) and
every read that names a comment's or a page's author answers ``null`` for them,
until a native edit makes the text the editor's own.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, as_user, bootstrap, make_app

from athena.aegis import comments as issue_comments
from athena.aegis import issues, projects
from athena.core import authorship, db, portability
from athena.mentor import page_comments, pages, spaces

WORD = "quokka"


def _user(conn, email: str, name: str, *, role: str = "member") -> int:
    cur = conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)", (email, name, role)
    )
    conn.commit()
    return int(cur.lastrowid)


def _bundles(tmp_path) -> tuple[dict, dict]:
    """A project with an issue and its comment, and a space with a page (edited
    once, so it has a version) and its comment, exported from another instance."""
    source = db.connect(tmp_path / "source.db")
    db.migrate(source)
    owner = _user(source, "owner@example.com", "Owner", role="admin")
    member = _user(source, "member@example.com", "Member")
    project = projects.create_project(
        source, name="Imported", key="IMP", created_by=owner, commit=True
    )
    issue = issues.create_issue(
        source,
        title="Imported issue",
        body="body",
        created_by=owner,
        project_id=project["id"],
        commit=True,
    )
    issue_comments.add_comment(
        source,
        issue_id=issue["id"],
        author_id=member,
        body=f"{WORD} said on the issue",
        commit=True,
    )
    space = spaces.create_space(
        source, key="IMS", name="Imported space", created_by=owner, commit=True
    )
    page = pages.create_page(
        source,
        space_id=space["id"],
        title="Imported page",
        body=f"{WORD} first text",
        created_by=member,
        commit=True,
    )
    pages.update_page(
        source,
        page["id"],
        editor_id=owner,
        body=f"{WORD} current text",
        commit=True,
    )
    page_comments.add_comment(
        source,
        page_id=page["id"],
        author_id=member,
        body=f"{WORD} said on the page",
        commit=True,
    )
    project_bundle = portability.export_bundle(source, "project", project["id"])
    space_bundle = portability.export_bundle(source, "space", space["id"])
    source.close()
    return project_bundle, space_bundle


@pytest.fixture
def imported(tmp_path):
    project_bundle, space_bundle = _bundles(tmp_path)
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        conn = db.connect(db_file)
        try:
            owner = _user(conn, "owner@example.com", "Owner", role="admin")
            member = _user(conn, "member@example.com", "Member")
            for bundle in (project_bundle, space_bundle):
                manifest = portability.build_import_manifest(conn, bundle)
                portability.replay_import_manifest(conn, bundle, manifest)
            issue_id = conn.execute(
                "SELECT i.id FROM issues i JOIN projects p ON p.id = i.project_id "
                "WHERE p.key = 'IMP'"
            ).fetchone()["id"]
            page_id = conn.execute(
                "SELECT id FROM pages WHERE title = 'Imported page'"
            ).fetchone()["id"]
            space_id = conn.execute(
                "SELECT id FROM spaces WHERE key = 'IMS'"
            ).fetchone()["id"]
        finally:
            conn.close()
        yield {
            "client": client,
            "db": db_file,
            "owner": owner,
            "member": member,
            "issue": issue_id,
            "page": page_id,
            "space": space_id,
        }


def _marks(db_file) -> dict[str, list]:
    conn = db.connect(db_file)
    try:
        return {
            table: [
                row["imported_at"]
                for row in conn.execute(f"SELECT imported_at FROM {table} ORDER BY id")
            ]
            for table in ("comments", "page_comments", "pages", "page_versions")
        }
    finally:
        conn.close()


def test_the_import_marks_the_text_it_writes(imported):
    marks = _marks(imported["db"])
    assert marks["comments"] and all(marks["comments"])
    assert marks["page_comments"] and all(marks["page_comments"])
    assert marks["pages"] and all(marks["pages"])
    assert marks["page_versions"] and all(marks["page_versions"])


def test_every_read_of_imported_text_names_no_author(imported):
    client, issue_id, page_id = (
        imported["client"],
        imported["issue"],
        imported["page"],
    )
    (comment,) = client.get(f"/issues/{issue_id}/comments", headers=ADMIN).json()[
        "comments"
    ]
    assert comment["author_id"] == imported["member"]
    assert comment["authored_by"] is None
    assert "imported_at" not in comment

    context = client.get(f"/issues/{issue_id}/work-context", headers=ADMIN).json()
    (item,) = context["comments"]["items"]
    assert item["authored_by"] is None
    assert "imported_at" not in item

    page = client.get(f"/pages/{page_id}", headers=ADMIN).json()
    assert page["updated_by"] == imported["owner"]
    assert page["authored_by"] is None
    listed = client.get(f"/spaces/{imported['space']}/pages", headers=ADMIN).json()
    rows = listed["pages"] if isinstance(listed, dict) else listed
    assert {row["id"]: row["authored_by"] for row in rows}[page_id] is None
    (version,) = client.get(f"/pages/{page_id}/versions", headers=ADMIN).json()
    assert version["authored_by"] is None
    one = client.get(f"/pages/{page_id}/versions/{version['version']}", headers=ADMIN)
    assert one.json()["authored_by"] is None
    (page_comment,) = client.get(f"/pages/{page_id}/comments", headers=ADMIN).json()
    assert page_comment["authored_by"] is None

    hits = client.get("/search", params={"q": WORD}, headers=ADMIN).json()
    by_kind = {hit["kind"]: hit["authored_by"] for hit in hits}
    assert by_kind["page"] is None
    # The import indexes issues and pages, not comments; a comment hit is
    # stamped by the same lookup either way.
    conn = db.connect(imported["db"])
    try:
        comment_hits = authorship.stamp_search_hits(
            conn,
            [
                {"kind": "issue_comment", "source_id": comment["id"]},
                {"kind": "page_comment", "source_id": page_comment["id"]},
            ],
            actor=None,
        )
    finally:
        conn.close()
    assert [hit["authored_by"] for hit in comment_hits] == [None, None]


def test_native_text_beside_imported_text_still_names_its_author(imported):
    client, issue_id = imported["client"], imported["issue"]
    native = client.post(
        f"/issues/{issue_id}/comments", json={"body": "written here"}, headers=ADMIN
    )
    assert native.status_code == 201, native.text
    assert native.json()["authored_by"] == {"id": 1, "is_agent": False}
    listed = client.get(f"/issues/{issue_id}/comments", headers=ADMIN).json()
    assert [row["authored_by"] for row in listed["comments"]] == [
        None,
        {"id": 1, "is_agent": False},
    ]
    assert _marks(imported["db"])["comments"][-1] is None


def test_a_native_edit_makes_the_text_its_editors_own(imported):
    client, issue_id, page_id = (
        imported["client"],
        imported["issue"],
        imported["page"],
    )
    member = imported["member"]
    as_member = as_user(member)

    (comment,) = client.get(f"/issues/{issue_id}/comments", headers=ADMIN).json()[
        "comments"
    ]
    edited = client.patch(
        f"/issues/{issue_id}/comments/{comment['id']}",
        json={"body": "rewritten here"},
        headers=as_member,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["authored_by"] == {"id": member, "is_agent": False}

    (page_comment,) = client.get(f"/pages/{page_id}/comments", headers=ADMIN).json()
    edited = client.patch(
        f"/pages/{page_id}/comments/{page_comment['id']}",
        json={"body": "rewritten here"},
        headers=as_member,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["authored_by"] == {"id": member, "is_agent": False}

    etag = client.get(f"/pages/{page_id}", headers=ADMIN).headers["ETag"]
    page = client.patch(
        f"/pages/{page_id}",
        json={"body": "written here"},
        headers={**ADMIN, "If-Match": etag},
    )
    assert page.status_code == 200, page.text
    assert page.json()["authored_by"] == {"id": 1, "is_agent": False}
    # The snapshot of the text it replaced is still the bundle's.
    versions = client.get(f"/pages/{page_id}/versions", headers=ADMIN).json()
    assert [row["authored_by"] for row in versions] == [None, None]

    marks = _marks(imported["db"])
    assert marks["comments"] == [None]
    assert marks["page_comments"] == [None]
    assert marks["pages"] == [None]
    assert all(marks["page_versions"])


def test_rows_imported_before_0094_are_not_guessed(tmp_path):
    """The migration adds the column and marks nothing: no record says which
    existing rows came from a bundle."""
    conn = db.connect(tmp_path / "upgrade.db")
    try:
        migrations = sorted(db.MIGRATIONS_DIR.glob("*.sql"))
        assert migrations[-1].name == "0094_imported_text.sql"
        for migration in migrations[:-1]:
            conn.executescript(migration.read_text())
        owner = _user(conn, "owner@example.com", "Owner", role="admin")
        project = projects.create_project(
            conn, name="Old", key="OLD", created_by=owner, commit=True
        )
        issue = issues.create_issue(
            conn,
            title="Old",
            body="",
            created_by=owner,
            project_id=project["id"],
            commit=True,
        )
        conn.execute(
            "INSERT INTO comments (issue_id, author_id, body) VALUES (?, ?, ?)",
            (issue["id"], owner, "older"),
        )
        conn.commit()
        conn.executescript(migrations[-1].read_text())
        assert [
            row["imported_at"]
            for row in conn.execute("SELECT imported_at FROM comments")
        ] == [None]
    finally:
        conn.close()
