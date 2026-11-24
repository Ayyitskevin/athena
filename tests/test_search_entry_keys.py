"""A write finds its own search entry by key; it never scans the index (H2).

``search_index`` is an FTS5 table, and its ``kind`` and ``source_id`` columns are
UNINDEXED: a ``DELETE ... WHERE kind = ? AND source_id = ?`` reads every entry ever
written. Every issue create and every comment ran that delete inside its write
transaction, so writes slowed as history grew (PERF-5: 0.7, 9.1 and 48.9 ms at 1,000,
10,000 and 50,000 issues). ``search_entries`` maps each source to its FTS rowid, and
``search.index_document`` is the one function that writes either table, so the
delete goes by rowid.

These tests pin, in the order the change must hold them:

  * the plan: no statement a write runs scans the FTS table (principle 12: tests
    assert query plans, not timings);
  * one writer: nothing outside ``core/search.py`` writes ``search_index``;
  * the answers: on a workspace with edits, deletes, an undo, an import and
    private and public containers, the index holds exactly the rows, with exactly
    the rowids, it held before the change, and every search answers as before for
    every caller (the expected values were recorded on the code before the
    change), with no duplicate and no orphan;
  * the migration: entries written before it are found, re-indexed and removed by
    key after it; running its work again changes nothing; an interrupted run leaves
    a database that starts and migrates.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from pathlib import Path
import re
import sqlite3

from fastapi.testclient import TestClient
import pytest

from athena.aegis import comment_commands, issues, projects
from athena.core import db, portability, search, users
from athena.main import create_app
from athena.mentor import page_comments, pages, spaces

from support import (
    ADMIN,
    add_user,
    as_user,
    bootstrap,
    index_search_without_entry_keys,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "athena"
CLEO = as_user(2)  # a member of the private project and the private space
OTTO = as_user(3)  # a member of neither


def _key_migration() -> Path:
    """This change's migration, found by name so no test depends on its number."""
    (found,) = db.MIGRATIONS_DIR.glob("*_search_entry_keys.sql")
    return found


class Recording:
    """A connection that remembers every statement it is asked to run."""

    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return self._inner.execute(sql, params)


def _plans_touching_the_index(
    conn: sqlite3.Connection, statements: list[tuple[str, tuple]]
) -> list[tuple[str, list[str]]]:
    found = []
    for sql, params in statements:
        if "search_index" not in sql and "search_entries" not in sql:
            continue
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
        found.append((" ".join(sql.split()), plan))
    return found


# --- the plan ----------------------------------------------------------------


def test_a_write_finds_its_search_entry_by_key_and_never_scans_the_index(tmp_path):
    """An issue create, a comment and an edit are the hot writes. Each may look its
    entry up and delete it, but only by key: the FTS table is reached by rowid
    (``INDEX 0:=``), never walked (``INDEX 0:`` with no constraint), because a walk
    reads every entry the workspace has ever had."""
    with closing(db.connect(tmp_path / "plan.db")) as conn:
        db.migrate(conn)
        users.create_user(conn, email="a@e.com", name="A", role="admin", commit=True)
        actor = users.get_user(conn, 1)
        for n in range(3):  # some history for the delete to (not) walk
            issues.create_issue(
                conn, title=f"old {n}", body="history", created_by=1, commit=True
            )
        recording = Recording(conn)
        issue = issues.create_issue(
            recording, title="new", body="fresh", created_by=1, commit=True
        )
        comment_commands.create_comment(
            recording, actor=actor, issue_id=issue["id"], body="a remark"
        )
        issues.update_issue(recording, issue["id"], body="edited", commit=True)
        plans = _plans_touching_the_index(conn, recording.statements)

    assert plans, "the writes did not index anything"
    walks = [
        (sql, plan)
        for sql, plan in plans
        if any(
            step.startswith("SCAN search_index VIRTUAL TABLE INDEX 0:")
            and not step.endswith("=")
            for step in plan
        )
    ]
    assert walks == [], walks
    deletes = [
        (sql, plan) for sql, plan in plans if sql.startswith("DELETE FROM search_index")
    ]
    # The edit replaced the issue's entry: that delete happened, by rowid.
    assert deletes, plans
    for sql, plan in deletes:
        assert plan == ["SCAN search_index VIRTUAL TABLE INDEX 0:="], (sql, plan)
    lookups = [(sql, plan) for sql, plan in plans if "FROM search_entries" in sql]
    assert lookups, plans
    for sql, plan in lookups:
        assert plan == [
            "SEARCH search_entries USING PRIMARY KEY (kind=? AND source_id=?)"
        ], (sql, plan)


# --- one writer ----------------------------------------------------------------

_INDEX_WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|DELETE\s+FROM|UPDATE|REPLACE\s+INTO)\s+"
    r"search_(index|entries)\b",
    re.IGNORECASE,
)


def test_only_the_search_module_writes_the_index():
    """The map is only right if every write goes through ``index_document``. A
    second writer (the importer had one, the page purge another) would add or
    remove an FTS row the map does not know about, and the next write of that
    source would then miss it: a duplicate hit, or a hit with nothing behind it."""
    writers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if _INDEX_WRITE.search(path.read_text(encoding="utf-8"))
    )
    assert writers == ["core/search.py"]


# --- the answers ----------------------------------------------------------------


def _source_bundle(tmp_path: Path) -> dict:
    """A small project, exported, for the workspace to import."""
    with closing(db.connect(tmp_path / "source.db")) as source:
        db.migrate(source)
        owner = users.create_user(
            source, email="a@e.com", name="Ann", role="admin", commit=True
        )
        project = projects.create_project(
            source, name="Imported", key="IMP", created_by=owner["id"], commit=True
        )
        first = issues.create_issue(
            source,
            title="Imported zephyr alpha",
            body="carried over",
            created_by=owner["id"],
            project_id=project["id"],
            commit=True,
        )
        issues.create_issue(
            source,
            title="Imported beta",
            body=f"zephyr follows [[issue:{first['id']}]]",
            created_by=owner["id"],
            project_id=project["id"],
            commit=True,
        )
        return portability.export_bundle(source, "project", project["id"])


def _space_bundle(tmp_path: Path) -> dict:
    with closing(db.connect(tmp_path / "source-space.db")) as source:
        db.migrate(source)
        owner = users.create_user(
            source, email="a@e.com", name="Ann", role="admin", commit=True
        )
        space = spaces.create_space(
            source,
            key="ISP",
            name="Imported space",
            created_by=owner["id"],
            commit=True,
        )
        pages.create_page(
            source,
            space_id=space["id"],
            title="Imported zephyr page",
            body="kestrel notes from elsewhere",
            created_by=owner["id"],
            commit=True,
        )
        return portability.export_bundle(source, "space", space["id"])


def _workspace(tmp_path: Path) -> Path:
    """Public and private containers, three people, and a history that edits,
    deletes, undoes and imports. Every write goes through the product's own
    routes and commands."""
    project_bundle = _source_bundle(tmp_path)
    space_bundle = _space_bundle(tmp_path)
    db_file = tmp_path / "workspace.db"
    with TestClient(create_app(db_file)) as c:
        bootstrap(c)
        add_user(c, "cleo@e.com", name="Cleo")
        add_user(c, "otto@e.com", name="Otto")

        def post(path, body=None, headers=ADMIN):
            response = c.post(path, json=body, headers=headers)
            assert response.status_code in (200, 201, 204), response.text
            return response.json() if response.content else None

        def patch(path, body, headers=ADMIN):
            response = c.patch(path, json=body, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        def delete(path, headers=ADMIN):
            response = c.delete(path, headers=headers)
            assert response.status_code == 204, response.text

        opn = post("/projects", {"name": "Open", "key": "OPN"}, CLEO)["id"]
        sec = post("/projects", {"name": "Secret", "key": "SEC"}, CLEO)["id"]
        c.put(
            f"/projects/{sec}/visibility", json={"visibility": "private"}, headers=CLEO
        )
        launch = post(
            "/issues",
            {
                "title": "Zephyr launch plan",
                "body": "rollout steps for zephyr",
                "project_id": opn,
            },
            CLEO,
        )["id"]
        budget = post(
            "/issues",
            {
                "title": "Zephyr secret budget",
                "body": "numbers only cleo sees",
                "project_id": sec,
            },
            CLEO,
        )["id"]
        idea = post("/issues", {"title": "Orphan zephyr idea", "body": "unfiled"})["id"]
        kestrel = post(
            "/issues",
            {
                "title": "Kestrel migration",
                "body": "move the kestrel tables",
                "project_id": opn,
            },
            CLEO,
        )["id"]
        patch(f"/issues/{launch}", {"title": "Zephyr launch runbook"}, CLEO)
        patch(f"/issues/{launch}", {"body": "rollout steps revised"}, CLEO)
        patch(f"/issues/{kestrel}", {"body": "kestrel zephyr cutover"}, CLEO)
        patch(f"/issues/{kestrel}", {"body": "kestrel zephyr cutover, again"}, CLEO)
        first = post(f"/issues/{launch}/comments", {"body": "zephyr comment one"}, CLEO)
        aside = post(f"/issues/{launch}/comments", {"body": "kestrel aside"}, CLEO)
        post(f"/issues/{budget}/comments", {"body": "secret zephyr note"}, CLEO)
        patch(
            f"/issues/{launch}/comments/{first['id']}",
            {"body": "zephyr comment edited"},
            CLEO,
        )
        delete(f"/issues/{launch}/comments/{aside['id']}", CLEO)
        post(f"/issues/{idea}/archive")
        archived = c.get("/activity", params={"verb": "archived"}, headers=ADMIN).json()
        post(f"/activity/{archived[0]['id']}/undo")
        post(f"/issues/{kestrel}/archive", headers=CLEO)

        doc = post("/spaces", {"key": "DOC", "name": "Docs"}, CLEO)["id"]
        prv = post("/spaces", {"key": "PRV", "name": "Private"}, CLEO)["id"]
        c.put(f"/spaces/{prv}/visibility", json={"visibility": "private"}, headers=CLEO)
        handbook = post(
            f"/spaces/{doc}/pages",
            {"title": "Zephyr handbook", "body": "how zephyr works"},
            CLEO,
        )["id"]
        post(
            f"/spaces/{prv}/pages",
            {"title": "Zephyr private notes", "body": "kestrel too"},
            CLEO,
        )
        doomed = post(
            f"/spaces/{doc}/pages",
            {"title": "Doomed zephyr page", "body": "to be deleted"},
            CLEO,
        )["id"]
        post(f"/pages/{doomed}/comments", {"body": "zephyr page comment doomed"}, CLEO)
        remark = post(
            f"/pages/{handbook}/comments", {"body": "zephyr handbook remark"}, CLEO
        )
        c.patch(
            f"/pages/{handbook}/comments/{remark['id']}",
            json={"body": "zephyr handbook remark, edited"},
            headers=CLEO,
        )
        patch(f"/pages/{handbook}", {"body": "how zephyr works now"}, CLEO)
        post(f"/pages/{handbook}/versions/1/restore", headers=CLEO)
        delete(f"/pages/{doomed}", CLEO)

    with closing(db.connect(db_file)) as conn:
        for bundle in (project_bundle, space_bundle):
            manifest = portability.build_import_manifest(conn, bundle)
            portability.replay_import_manifest(conn, bundle, manifest)
    return db_file


_QUERIES: tuple[tuple[str, dict], ...] = (
    ("zephyr", {}),
    ("zeph", {}),
    ("kestrel", {}),
    ("comment", {}),
    ("secret", {}),
    ("imported", {}),
    ("zephyr", {"kind": "issue"}),
    ("zephyr", {"kind": "page_comment"}),
    ("zephyr", {"include_archived": True}),
    ("zephyr kestrel", {"title_any": True}),
    ("zephyr", {"limit": 3, "offset": 2}),
    ("zephyr", {"kind": "issue", "ids": [1, 2, 3, 4]}),
)


def _answers(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every query, as every kind of caller asks it: hits as ``kind:id``."""
    callers: dict[str, object] = {
        "internal": "ungated",
        "admin": users.get_user(conn, 1),
        "cleo": users.get_user(conn, 2),
        "otto": users.get_user(conn, 3),
        "anonymous": None,
    }
    answers: dict[str, list[str]] = {}
    for name, actor in callers.items():
        for query, options in _QUERIES:
            kwargs = dict(options)
            if actor != "ungated":
                kwargs["actor"] = actor
            hits = search.search(conn, query, **kwargs)
            label = f"{name} {query!r} {sorted(options.items())}"
            answers[label] = [f"{h['kind']}:{h['source_id']}" for h in hits]
    return answers


def _index_rows(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT rowid, kind, source_id, title, body FROM search_index ORDER BY rowid"
        )
    ]


def test_search_answers_and_the_index_are_unchanged_on_a_workspace_with_history(
    tmp_path,
):
    db_file = _workspace(tmp_path)
    with closing(db.connect(db_file)) as conn:
        answers = _answers(conn)
        rows = _index_rows(conn)
        live = {
            (kind, row[0])
            for kind, table in (
                ("issue", "issues"),
                ("page", "pages"),
                ("issue_comment", "comments"),
                ("page_comment", "page_comments"),
            )
            for row in conn.execute(f"SELECT id FROM {table}")
        }
        keys = {
            (row[0], row[1]): row[2]
            for row in conn.execute(
                "SELECT kind, source_id, fts_rowid FROM search_entries"
            )
        }

    for label, hits in answers.items():
        duplicates = [hit for hit, n in Counter(hits).items() if n > 1]
        assert duplicates == [], f"{label}: a duplicate search result {duplicates}"
    per_source = Counter((kind, source_id) for _, kind, source_id, _, _ in rows)
    assert [key for key, n in per_source.items() if n > 1] == [], "duplicate entries"
    # Imported comments are not indexed (they were not before this change either),
    # so the indexed sources are the live ones less those.
    assert set(per_source) <= live, "an entry with no live source (an orphan)"
    # The map names every entry, and nothing else.
    assert keys == {(kind, source_id): rowid for rowid, kind, source_id, _, _ in rows}

    assert rows == EXPECTED_INDEX_ROWS
    assert answers == EXPECTED_ANSWERS


# Recorded on cdf2c49, before the change, from the same workspace.
EXPECTED_INDEX_ROWS: list[tuple] = [
    (2, "issue", 2, "Zephyr secret budget", "numbers only cleo sees"),
    (3, "issue", 3, "Orphan zephyr idea", "unfiled"),
    (5, "issue", 1, "Zephyr launch runbook", "rollout steps revised"),
    (6, "issue", 4, "Kestrel migration", "kestrel zephyr cutover, again"),
    (9, "issue_comment", 3, "", "secret zephyr note"),
    (10, "issue_comment", 1, "", "zephyr comment edited"),
    (12, "page", 2, "Zephyr private notes", "kestrel too"),
    (15, "page_comment", 2, "", "zephyr handbook remark, edited"),
    (16, "page", 1, "Zephyr handbook", "how zephyr works"),
    (17, "issue", 5, "Imported zephyr alpha", "carried over"),
    (18, "issue", 6, "Imported beta", "zephyr follows [[issue:5]]"),
    (19, "page", 4, "Imported zephyr page", "kestrel notes from elsewhere"),
]
EXPECTED_ANSWERS: dict[str, list[str]] = {
    "internal 'zephyr' []": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "internal 'zeph' []": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "internal 'kestrel' []": ["page:2", "page:4"],
    "internal 'comment' []": ["issue_comment:1"],
    "internal 'secret' []": ["issue:2", "issue_comment:3"],
    "internal 'imported' []": ["issue:5", "issue:6", "page:4"],
    "internal 'zephyr' [('kind', 'issue')]": [
        "issue:3",
        "issue:5",
        "issue:1",
        "issue:2",
        "issue:6",
    ],
    "internal 'zephyr' [('kind', 'page_comment')]": ["page_comment:2"],
    "internal 'zephyr' [('include_archived', True)]": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:4",
        "issue:6",
    ],
    "internal 'zephyr kestrel' [('title_any', True)]": [
        "issue:3",
        "page:2",
        "page:1",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
    ],
    "internal 'zephyr' [('limit', 3), ('offset', 2)]": ["page:2", "issue:5", "issue:1"],
    "internal 'zephyr' [('ids', [1, 2, 3, 4]), ('kind', 'issue')]": [
        "issue:3",
        "issue:1",
        "issue:2",
    ],
    "admin 'zephyr' []": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "admin 'zeph' []": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "admin 'kestrel' []": ["page:2", "page:4"],
    "admin 'comment' []": ["issue_comment:1"],
    "admin 'secret' []": ["issue:2", "issue_comment:3"],
    "admin 'imported' []": ["issue:5", "issue:6", "page:4"],
    "admin 'zephyr' [('kind', 'issue')]": [
        "issue:3",
        "issue:5",
        "issue:1",
        "issue:2",
        "issue:6",
    ],
    "admin 'zephyr' [('kind', 'page_comment')]": ["page_comment:2"],
    "admin 'zephyr' [('include_archived', True)]": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:4",
        "issue:6",
    ],
    "admin 'zephyr kestrel' [('title_any', True)]": [
        "issue:3",
        "page:2",
        "page:1",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
    ],
    "admin 'zephyr' [('limit', 3), ('offset', 2)]": ["page:2", "issue:5", "issue:1"],
    "admin 'zephyr' [('ids', [1, 2, 3, 4]), ('kind', 'issue')]": [
        "issue:3",
        "issue:1",
        "issue:2",
    ],
    "cleo 'zephyr' []": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "cleo 'zeph' []": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "cleo 'kestrel' []": ["page:2", "page:4"],
    "cleo 'comment' []": ["issue_comment:1"],
    "cleo 'secret' []": ["issue:2", "issue_comment:3"],
    "cleo 'imported' []": ["issue:5", "issue:6", "page:4"],
    "cleo 'zephyr' [('kind', 'issue')]": [
        "issue:3",
        "issue:5",
        "issue:1",
        "issue:2",
        "issue:6",
    ],
    "cleo 'zephyr' [('kind', 'page_comment')]": ["page_comment:2"],
    "cleo 'zephyr' [('include_archived', True)]": [
        "page:1",
        "issue:3",
        "page:2",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
        "issue_comment:3",
        "issue_comment:1",
        "page_comment:2",
        "issue:4",
        "issue:6",
    ],
    "cleo 'zephyr kestrel' [('title_any', True)]": [
        "issue:3",
        "page:2",
        "page:1",
        "issue:5",
        "issue:1",
        "issue:2",
        "page:4",
    ],
    "cleo 'zephyr' [('limit', 3), ('offset', 2)]": ["page:2", "issue:5", "issue:1"],
    "cleo 'zephyr' [('ids', [1, 2, 3, 4]), ('kind', 'issue')]": [
        "issue:3",
        "issue:1",
        "issue:2",
    ],
    "otto 'zephyr' []": [
        "page:1",
        "issue:3",
        "issue:5",
        "issue:1",
        "page:4",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "otto 'zeph' []": [
        "page:1",
        "issue:3",
        "issue:5",
        "issue:1",
        "page:4",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "otto 'kestrel' []": ["page:4"],
    "otto 'comment' []": ["issue_comment:1"],
    "otto 'secret' []": [],
    "otto 'imported' []": ["issue:5", "issue:6", "page:4"],
    "otto 'zephyr' [('kind', 'issue')]": ["issue:3", "issue:5", "issue:1", "issue:6"],
    "otto 'zephyr' [('kind', 'page_comment')]": ["page_comment:2"],
    "otto 'zephyr' [('include_archived', True)]": [
        "page:1",
        "issue:3",
        "issue:5",
        "issue:1",
        "page:4",
        "issue_comment:1",
        "page_comment:2",
        "issue:4",
        "issue:6",
    ],
    "otto 'zephyr kestrel' [('title_any', True)]": [
        "issue:3",
        "page:1",
        "issue:5",
        "issue:1",
        "page:4",
    ],
    "otto 'zephyr' [('limit', 3), ('offset', 2)]": ["issue:5", "issue:1", "page:4"],
    "otto 'zephyr' [('ids', [1, 2, 3, 4]), ('kind', 'issue')]": ["issue:3", "issue:1"],
    "anonymous 'zephyr' []": [
        "page:1",
        "issue:3",
        "issue:5",
        "issue:1",
        "page:4",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "anonymous 'zeph' []": [
        "page:1",
        "issue:3",
        "issue:5",
        "issue:1",
        "page:4",
        "issue_comment:1",
        "page_comment:2",
        "issue:6",
    ],
    "anonymous 'kestrel' []": ["page:4"],
    "anonymous 'comment' []": ["issue_comment:1"],
    "anonymous 'secret' []": [],
    "anonymous 'imported' []": ["issue:5", "issue:6", "page:4"],
    "anonymous 'zephyr' [('kind', 'issue')]": [
        "issue:3",
        "issue:5",
        "issue:1",
        "issue:6",
    ],
    "anonymous 'zephyr' [('kind', 'page_comment')]": ["page_comment:2"],
    "anonymous 'zephyr' [('include_archived', True)]": [
        "page:1",
        "issue:3",
        "issue:5",
        "issue:1",
        "page:4",
        "issue_comment:1",
        "page_comment:2",
        "issue:4",
        "issue:6",
    ],
    "anonymous 'zephyr kestrel' [('title_any', True)]": [
        "issue:3",
        "page:1",
        "issue:5",
        "issue:1",
        "page:4",
    ],
    "anonymous 'zephyr' [('limit', 3), ('offset', 2)]": [
        "issue:5",
        "issue:1",
        "page:4",
    ],
    "anonymous 'zephyr' [('ids', [1, 2, 3, 4]), ('kind', 'issue')]": [
        "issue:3",
        "issue:1",
    ],
}


# --- the migration ------------------------------------------------------------


@pytest.fixture
def before_the_key_migration(tmp_path, monkeypatch):
    """Point the runner at the migrations before this change's; return the
    inventory through it. Neither depends on its number or on it being newest."""
    source = db.MIGRATIONS_DIR
    mine = _key_migration()
    through = tmp_path / "through"
    before = tmp_path / "before"
    through.mkdir()
    before.mkdir()
    for migration in sorted(source.glob("*.sql")):
        if migration.name <= mine.name:
            (through / migration.name).write_bytes(migration.read_bytes())
        if migration.name < mine.name:
            (before / migration.name).write_bytes(migration.read_bytes())
    monkeypatch.setattr(db, "MIGRATIONS_DIR", before)
    return through


def _history_before_the_migration(conn: sqlite3.Connection) -> dict:
    users.create_user(conn, email="a@e.com", name="A", role="admin", commit=True)
    space = spaces.create_space(conn, key="S", name="S", created_by=1, commit=True)
    first = issues.create_issue(
        conn, title="Heron first", body="old body", created_by=1, commit=True
    )
    second = issues.create_issue(
        conn, title="Heron second", body="also old", created_by=1, commit=True
    )
    issues.update_issue(conn, first["id"], body="old body, edited once", commit=True)
    page = pages.create_page(
        conn,
        space_id=space["id"],
        title="Heron page",
        body="p",
        created_by=1,
        commit=True,
    )
    comment = comment_commands.create_comment(
        conn, actor=users.get_user(conn, 1), issue_id=second["id"], body="heron chat"
    )
    note = page_comments.add_comment(
        conn, page_id=page["id"], author_id=1, body="heron margin", commit=True
    )
    search.index_document(conn, kind="page_comment", source_id=note["id"], commit=True)
    return {
        "first": first["id"],
        "second": second["id"],
        "page": page["id"],
        "comment": comment["id"],
    }


def test_entries_written_before_the_migration_are_found_and_replaced_by_key(
    tmp_path, monkeypatch, before_the_key_migration
):
    with monkeypatch.context() as old:
        index_search_without_entry_keys(old)
        with closing(db.connect(tmp_path / "old.db")) as conn:
            db.migrate(conn)
            made = _history_before_the_migration(conn)
            before = _index_rows(conn)

    monkeypatch.setattr(db, "MIGRATIONS_DIR", before_the_key_migration)
    with closing(db.connect(tmp_path / "old.db")) as conn:
        assert db.migrate(conn) == [_key_migration().name]
        # The migration maps what was there and changes nothing it maps.
        assert _index_rows(conn) == before
        assert {
            (row[0], row[1]): row[2]
            for row in conn.execute(
                "SELECT kind, source_id, fts_rowid FROM search_entries"
            )
        } == {(kind, source_id): rowid for rowid, kind, source_id, _, _ in before}
        # Entries written before the migration are found as they were.
        assert sorted(
            h["source_id"] for h in search.search(conn, "heron", kind="issue")
        ) == sorted([made["first"], made["second"]])

        # An old entry is replaced, not duplicated, by an edit after the migration.
        issues.update_issue(conn, made["first"], title="Egret first", commit=True)
        assert [h["source_id"] for h in search.search(conn, "egret")] == [made["first"]]
        assert made["first"] not in [
            h["source_id"] for h in search.search(conn, "heron", kind="issue")
        ]
        # And removed, not orphaned, by a delete after the migration.
        actor = users.get_user(conn, 1)
        assert comment_commands.delete_comment(
            conn, actor=actor, issue_id=made["second"], comment_id=made["comment"]
        )
        assert search.search(conn, "chat") == []
        pages.delete_page(conn, made["page"])
        assert search.search(conn, "margin") == []
        per_source = Counter(
            (row[0], row[1])
            for row in conn.execute("SELECT kind, source_id FROM search_index")
        )
        assert max(per_source.values()) == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM search_entries e WHERE NOT EXISTS "
                "(SELECT 1 FROM search_index s WHERE s.rowid = e.fts_rowid)"
            ).fetchone()[0]
            == 0
        )


def test_running_the_key_migration_again_changes_nothing(
    tmp_path, monkeypatch, before_the_key_migration
):
    with monkeypatch.context() as old:
        index_search_without_entry_keys(old)
        with closing(db.connect(tmp_path / "again.db")) as conn:
            db.migrate(conn)
            _history_before_the_migration(conn)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", before_the_key_migration)
    with closing(db.connect(tmp_path / "again.db")) as conn:
        db.migrate(conn)
        once = (_index_rows(conn), list(conn.execute("SELECT * FROM search_entries")))
        conn.executescript(
            "BEGIN;\n" + _key_migration().read_text(encoding="utf-8") + "\nCOMMIT;"
        )
        twice = (_index_rows(conn), list(conn.execute("SELECT * FROM search_entries")))
    assert [list(map(tuple, part)) for part in twice] == [
        list(map(tuple, part)) for part in once
    ]


def test_an_interrupted_key_migration_leaves_a_database_that_starts(
    tmp_path, monkeypatch, before_the_key_migration
):
    db_file = tmp_path / "interrupted.db"
    with monkeypatch.context() as old:
        index_search_without_entry_keys(old)
        with closing(db.connect(db_file)) as conn:
            db.migrate(conn)
            _history_before_the_migration(conn)
            before = _index_rows(conn)

    monkeypatch.setattr(db, "MIGRATIONS_DIR", before_the_key_migration)

    def counting(conn, stop_at=None):
        """Count the VM steps the migration's own transaction runs; stop at one."""
        steps = {"n": 0}

        def step() -> int:
            if conn.in_transaction:
                steps["n"] += 1
            return 1 if stop_at is not None and steps["n"] >= stop_at else 0

        conn.set_progress_handler(step, 1)
        return steps

    # Learn how long the migration runs on a copy, then stop the real one halfway.
    with (
        closing(db.connect(db_file)) as conn,
        closing(db.connect(tmp_path / "copy.db")) as copy,
    ):
        conn.backup(copy)
        whole = counting(copy)
        db.migrate(copy)
        copy.set_progress_handler(None, 0)
        assert whole["n"] > 100, whole
    with closing(db.connect(db_file)) as conn:
        counting(conn, stop_at=whole["n"] // 2)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            db.migrate(conn)
        conn.set_progress_handler(None, 0)
        # Nothing of it stuck: no ledger row, no map, the index as it was.
        assert (
            _key_migration().name
            in db.migration_status(conn, require_current=False).pending
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'search_entries'"
            ).fetchone()[0]
            == 0
        )
        assert _index_rows(conn) == before

    # The app starts on it, migrating as it does, and the old entries are found.
    with TestClient(create_app(db_file)) as c:
        assert c.get("/readyz").status_code == 200
    with closing(db.connect(db_file)) as conn:
        assert db.migration_status(conn).current
        assert len(search.search(conn, "heron", kind="issue")) == 2


# --- a map that went stale: the index was written around the indexer ------------


def _fresh(tmp_path: Path, name: str) -> sqlite3.Connection:
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    users.create_user(conn, email="a@e.com", name="A", role="admin", commit=True)
    return conn


def _hits(conn: sqlite3.Connection, word: str) -> list[tuple[str, int]]:
    return sorted(
        (hit["kind"], hit["source_id"]) for hit in search.search(conn, word, limit=50)
    )


def test_a_stale_map_row_does_not_refuse_the_next_write(tmp_path):
    """A map row that names a rowid whose entry is gone (an older process wrote
    the index after the migration) made the next write fail on the rowid's
    uniqueness, roll back, and fail again on the same rowid: every new issue
    answered 500. The map write replaces on either key."""
    with closing(_fresh(tmp_path, "stale.db")) as conn:
        issues.create_issue(
            conn, title="Plover one", body="b", created_by=1, commit=True
        )
        next_rowid = conn.execute("SELECT MAX(rowid) + 1 FROM search_index").fetchone()[
            0
        ]
        conn.execute(
            "INSERT INTO search_entries (kind, source_id, fts_rowid) VALUES (?, ?, ?)",
            ("page", 987_654, next_rowid),
        )
        conn.commit()
        made = issues.create_issue(
            conn, title="Plover two", body="b", created_by=1, commit=True
        )
        assert ("issue", made["id"]) in _hits(conn, "plover")
        named = conn.execute(
            "SELECT kind, source_id FROM search_entries WHERE fts_rowid = ?",
            (next_rowid,),
        ).fetchall()
        assert [tuple(row) for row in named] == [("issue", made["id"])]


def test_a_stale_map_row_never_deletes_another_sources_entry(tmp_path):
    """The rowid delete also names the kind and the source: a map row pointing
    at another source's entry must not remove that entry."""
    with closing(_fresh(tmp_path, "guard.db")) as conn:
        first = issues.create_issue(
            conn, title="Avocet first", body="b", created_by=1, commit=True
        )
        other = issues.create_issue(
            conn, title="Avocet other", body="b", created_by=1, commit=True
        )
        others_rowid = conn.execute(
            "SELECT fts_rowid FROM search_entries WHERE kind = 'issue' AND source_id = ?",
            (other["id"],),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM search_entries WHERE kind = 'issue' AND source_id = ?",
            (other["id"],),
        )
        conn.execute(
            "UPDATE search_entries SET fts_rowid = ? "
            "WHERE kind = 'issue' AND source_id = ?",
            (others_rowid, first["id"]),
        )
        conn.commit()
        issues.update_issue(conn, first["id"], body="edited", commit=True)
        assert ("issue", other["id"]) in _hits(conn, "avocet")


def _old_database_with(
    tmp_path: Path, monkeypatch, name: str, tamper
) -> tuple[Path, dict]:
    """A database at the schema before the key migration, with history written
    by the indexer of that time, then changed by ``tamper``."""
    path = tmp_path / name
    with monkeypatch.context() as old:
        index_search_without_entry_keys(old)
        with closing(db.connect(path)) as conn:
            db.migrate(conn)
            made = _history_before_the_migration(conn)
            tamper(conn, made)
            conn.commit()
    return path, made


def test_the_migration_keeps_the_newest_entry_of_a_source_and_removes_the_older(
    tmp_path, monkeypatch, before_the_key_migration
):
    """Two entries for one source (a replayed backfill): the newest is the one
    the source's text was last indexed as. The older must not stay searchable."""

    def duplicate(conn, made):
        # The older entry of the source keeps a rowid below the current one.
        current = conn.execute(
            "SELECT rowid, title, body FROM search_index "
            "WHERE kind = 'issue' AND source_id = ?",
            (made["second"],),
        ).fetchone()
        conn.execute("DELETE FROM search_index WHERE rowid = ?", (current["rowid"],))
        conn.execute(
            "INSERT INTO search_index (kind, source_id, title, body) "
            "VALUES ('issue', ?, 'Stale petrel', 'superseded')",
            (made["second"],),
        )
        conn.execute(
            "INSERT INTO search_index (kind, source_id, title, body) "
            "VALUES ('issue', ?, ?, ?)",
            (made["second"], current["title"], current["body"]),
        )

    path, made = _old_database_with(tmp_path, monkeypatch, "dup.db", duplicate)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", before_the_key_migration)
    with closing(db.connect(path)) as conn:
        assert _hits(conn, "petrel") == [("issue", made["second"])]
        db.migrate(conn)
        assert _hits(conn, "petrel") == []
        assert ("issue", made["second"]) in _hits(conn, "heron")
        entries = Counter(
            (row["kind"], row["source_id"])
            for row in conn.execute("SELECT kind, source_id FROM search_index")
        )
        assert set(entries.values()) == {1}
        mapped = {
            tuple(row)
            for row in conn.execute(
                "SELECT kind, source_id, fts_rowid FROM search_entries"
            )
        }
        assert mapped == {
            tuple(row)
            for row in conn.execute("SELECT kind, source_id, rowid FROM search_index")
        }


def test_run_again_on_a_drifted_map_the_migration_loses_no_sources_entry(
    tmp_path, monkeypatch, before_the_key_migration
):
    """A map row naming a rowid that now belongs to another source (an older
    process deleted and created after the migration). Run again by hand, the
    file must map both sources: left in place, the wrong row kept the rowid's
    real owner out of the map and the last pass deleted the mapped source's own
    entry as a duplicate."""
    path, made = _old_database_with(
        tmp_path, monkeypatch, "drift.db", lambda conn, made: None
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", before_the_key_migration)
    with closing(db.connect(path)) as conn:
        db.migrate(conn)
        rowid_of = {
            (row["kind"], row["source_id"]): row["fts_rowid"]
            for row in conn.execute("SELECT * FROM search_entries")
        }
        first, second = ("issue", made["first"]), ("issue", made["second"])
        conn.execute(
            "DELETE FROM search_entries WHERE kind = ? AND source_id = ?", second
        )
        conn.execute(
            "UPDATE search_entries SET fts_rowid = ? WHERE kind = ? AND source_id = ?",
            (rowid_of[second], *first),
        )
        conn.commit()
        conn.executescript(
            "BEGIN;\n" + _key_migration().read_text(encoding="utf-8") + "\nCOMMIT;"
        )
        hits = _hits(conn, "heron")
        assert first in hits and second in hits
        again = {
            (row["kind"], row["source_id"]): row["fts_rowid"]
            for row in conn.execute("SELECT * FROM search_entries")
        }
        assert again == rowid_of
