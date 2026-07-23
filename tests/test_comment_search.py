"""Comments are searchable — the knowledge-layer half of Phase 3 item 6.

Discussion is content: an answer, a decision, a caveat often lives in a comment, not
the doc body. These tests encode that contract:

  * posting an issue/page comment makes its text findable, and the hit borrows its
    PARENT's title and links to the parent (a comment has no page of its own);
  * editing re-indexes (old text stops matching), deleting removes the entry, and a
    page hard-delete takes its comments' entries with it (no dangling hits);
  * a comment inherits its parent's audience — it never surfaces to someone who can't
    see the parent issue/page, and it drops out when the parent is archived;
  * the pre-existing backfill makes comments written before this feature searchable.
"""

from fastapi.testclient import TestClient

from athena.aegis import comment_commands, issues
from athena.core import db, search
from athena.main import create_app
from athena.mentor import (
    page_comment_commands,
    pages,
    space_commands,
    spaces,
)


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn, email="a@e.com", name="A", role="member"):
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)", (email, name, role)
    )
    conn.commit()


# --- unit: comments follow writes and link to their parent ------------------


def test_issue_comment_is_findable_and_borrows_parent(tmp_path):
    conn = _migrated_conn(tmp_path / "ic.db")
    _seed_user(conn)
    iss = issues.create_issue(conn, title="Deploy runbook", body="", created_by=1)
    comment_commands.create_comment(
        conn, actor_id=1, issue_id=iss["id"], body="rotate the zephyr token first"
    )
    hits = search.search(conn, "zephyr")
    assert len(hits) == 1
    h = hits[0]
    assert h["kind"] == "issue_comment"
    assert h["title"] == "Deploy runbook"  # borrows the parent issue's title
    assert h["parent_kind"] == "issue" and h["parent_id"] == iss["id"]


def test_page_comment_is_findable_and_borrows_parent(tmp_path):
    conn = _migrated_conn(tmp_path / "pc.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    pg = pages.create_page(conn, space_id=sp["id"], title="Onboarding", created_by=1)
    page_comment_commands.create_page_comment(
        conn, actor_id=1, page_id=pg["id"], body="the zephyr checklist lives here"
    )
    hits = search.search(conn, "zephyr")
    assert len(hits) == 1
    h = hits[0]
    assert h["kind"] == "page_comment"
    assert h["title"] == "Onboarding"
    assert h["parent_kind"] == "page" and h["parent_id"] == pg["id"]


def test_editing_a_comment_reindexes(tmp_path):
    conn = _migrated_conn(tmp_path / "edit.db")
    _seed_user(conn)
    iss = issues.create_issue(conn, title="I", body="", created_by=1)
    c = comment_commands.create_comment(
        conn, actor_id=1, issue_id=iss["id"], body="the zephyr note"
    )
    comment_commands.edit_comment(
        conn,
        actor_id=1,
        issue_id=iss["id"],
        comment_id=c["id"],
        body="the borealis note",
    )
    assert search.search(conn, "zephyr") == []  # old text no longer matches
    assert [h["kind"] for h in search.search(conn, "borealis")] == ["issue_comment"]


def test_deleting_a_comment_removes_it_from_search(tmp_path):
    conn = _migrated_conn(tmp_path / "del.db")
    _seed_user(conn)
    iss = issues.create_issue(conn, title="I", body="", created_by=1)
    c = comment_commands.create_comment(
        conn, actor_id=1, issue_id=iss["id"], body="the zephyr note"
    )
    comment_commands.delete_comment(
        conn, actor_id=1, issue_id=iss["id"], comment_id=c["id"]
    )
    assert search.search(conn, "zephyr") == []


def test_page_hard_delete_clears_its_comment_index(tmp_path):
    # A hard-deleted page takes its comments with it — their FTS entries must not dangle.
    conn = _migrated_conn(tmp_path / "purge.db")
    _seed_user(conn)
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    pg = pages.create_page(conn, space_id=sp["id"], title="Doc", created_by=1)
    page_comment_commands.create_page_comment(
        conn, actor_id=1, page_id=pg["id"], body="the zephyr checklist"
    )
    pages.delete_page(conn, pg["id"])
    assert search.search(conn, "zephyr") == []
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM search_index WHERE kind = 'page_comment'"
    ).fetchone()["n"]
    assert left == 0


# --- visibility: a comment inherits its parent's audience -------------------


def test_page_comment_hidden_from_non_member_of_private_space(tmp_path):
    conn = _migrated_conn(tmp_path / "vis.db")
    _seed_user(conn, email="own@e.com", name="Own")  # id 1 (creator/member)
    _seed_user(conn, email="out@e.com", name="Out")  # id 2 (outsider)
    sp = spaces.create_space(conn, key="SEC", name="Sec", created_by=1)
    pg = pages.create_page(conn, space_id=sp["id"], title="Secret", created_by=1)
    page_comment_commands.create_page_comment(
        conn, actor_id=1, page_id=pg["id"], body="zephyr rotation steps"
    )
    space_commands.set_space_visibility(
        conn, actor_id=1, space_id=sp["id"], visibility="private"
    )
    creator = {"id": 1, "role": "member"}
    outsider = {"id": 2, "role": "member"}
    assert [h["kind"] for h in search.search(conn, "zephyr", actor=creator)] == [
        "page_comment"
    ]
    assert search.search(conn, "zephyr", actor=outsider) == []  # no leak


def test_comment_on_archived_parent_drops_out(tmp_path):
    conn = _migrated_conn(tmp_path / "arch.db")
    _seed_user(conn, role="admin")
    iss = issues.create_issue(conn, title="I", body="", created_by=1)
    comment_commands.create_comment(
        conn, actor_id=1, issue_id=iss["id"], body="the zephyr note"
    )
    admin = {"id": 1, "role": "admin"}
    assert [h["kind"] for h in search.search(conn, "zephyr", actor=admin)] == [
        "issue_comment"
    ]
    issues.set_archived(conn, iss["id"], True)
    assert search.search(conn, "zephyr", actor=admin) == []  # archived parent hides it


def test_kind_filter_excludes_comments(tmp_path):
    # kind='issue' narrows to the issues themselves — a filtered issue search (and its
    # per-id restriction) must not sweep in comment hits.
    conn = _migrated_conn(tmp_path / "kind.db")
    _seed_user(conn)
    iss = issues.create_issue(conn, title="zephyr build", body="", created_by=1)
    comment_commands.create_comment(
        conn, actor_id=1, issue_id=iss["id"], body="another zephyr mention"
    )
    kinds = {h["kind"] for h in search.search(conn, "zephyr", kind="issue")}
    assert kinds == {"issue"}


# --- backfill: comments written before the feature are searchable -----------


def test_backfill_indexes_preexisting_comments(tmp_path):
    # The 0053 migration backfills existing comments (like 0013 did for issues/pages).
    # Simulate a legacy comment (a row with no FTS entry) and run the backfill SQL.
    conn = _migrated_conn(tmp_path / "backfill.db")
    _seed_user(conn)
    iss = issues.create_issue(conn, title="I", body="", created_by=1)
    # Insert a comment DIRECTLY, bypassing the command — so it has no search entry.
    conn.execute(
        "INSERT INTO comments (issue_id, author_id, body) VALUES (?, 1, ?)",
        (iss["id"], "legacy zephyr note"),
    )
    conn.commit()
    assert search.search(conn, "zephyr") == []  # not indexed yet
    conn.execute(
        "INSERT INTO search_index (kind, source_id, title, body) "
        "SELECT 'issue_comment', id, '', body FROM comments "
        "WHERE id NOT IN (SELECT source_id FROM search_index WHERE kind='issue_comment')"
    )
    conn.commit()
    assert [h["kind"] for h in search.search(conn, "zephyr")] == ["issue_comment"]


# --- web: /find links a comment hit to its parent ---------------------------


def _login(client, email="a@e.com", name="A"):
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})


def test_find_links_comment_hit_to_its_parent(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        _login(client)
        h = {"X-Athena-Actor": "1"}
        iss = client.post("/issues", json={"title": "Runbook"}, headers=h).json()
        client.post(
            f"/issues/{iss['id']}/comments",
            json={"body": "the zephyr rotation lives here"},
            headers=h,
        )
        html = client.get("/find", params={"q": "zephyr"}).text
        # The comment hit links to the PARENT issue (not a bogus page id) and is badged.
        assert f'href="/aegis/issues/{iss["id"]}"' in html
        assert "comment" in html
        assert "Runbook" in html  # borrowed parent title
