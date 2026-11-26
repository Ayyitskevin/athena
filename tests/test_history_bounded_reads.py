"""The project brief and a search for a common word do not read history (H5).

PERF-8 found three reads linear in history. Two are bounded here; the third,
the idle sort, is not (docs/PERFORMANCE.md says why and what it would take).

- **The project brief's latest completions** grouped every issue event in the
  workspace by issue to find each issue's newest completion: a walk of the
  ``(target_kind, id)`` index over the whole issue trail, then a sort. It now asks
  each of the project's issues for its own newest visible native completion, down
  ``idx_activity_target``, newest first, stopping at the first one.
- **Full-text search** scored every entry that contains the word before taking a
  page, and built the list of every visible and every archived issue, comment and
  page to filter them. A search now ranks the newest ``RANK_WINDOW`` matching
  entries it may return, and asks each entry's own row whether it is visible and
  not archived.

House style (docs/PERFORMANCE.md): the plan of each hot statement names what must
not appear in it, with and without statistics, and where a plan cannot show how
far a read goes, a test counts SQLite's virtual-machine steps on a small and a
large history. Nothing here is timed.
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

import pytest

from athena.aegis import issues, projects
from athena.aegis.project_brief import build_project_brief
from athena.core import activity, db, search


@pytest.fixture
def ws(tmp_path):
    path = tmp_path / "h5.db"
    conn = db.connect(path)
    db.migrate(conn)
    for name, role in [("Owner", "admin"), ("Member", "member"), ("Worker", "member")]:
        conn.execute(
            "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
            (f"{name.lower()}@example.invalid", name, role),
        )
    conn.commit()
    yield path, conn
    conn.close()


def _user(conn: sqlite3.Connection, user_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def _issue(conn, project_id, title="Work", body="plain body"):
    return issues.create_issue(
        conn, title=title, body=body, created_by=1, project_id=project_id, commit=True
    )


def _event(conn, issue_id, verb, detail=""):
    return activity.record(
        conn,
        actor_id=3,
        verb=verb,
        target_kind="issue",
        target_id=issue_id,
        detail=detail,
        commit=True,
    )


def _complete(conn, issue_id):
    return _event(conn, issue_id, "claim_completed", "generation " + "a" * 32)


class Recording:
    """A connection that remembers every statement it is asked to run."""

    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.statements: list[tuple[str, list]] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self._inner.execute(sql, params)


def _plan(conn: sqlite3.Connection, sql: str, params: list) -> list[str]:
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _steps(conn: sqlite3.Connection, read) -> int:
    """SQLite virtual-machine steps one read takes: work, not time."""
    count = [0]

    def tick() -> int:
        count[0] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        read()
    finally:
        conn.set_progress_handler(None, 1)
    return count[0]


#: What ANALYZE wrote for the indexes these statements choose between on the
#: seeded 10,000-issue fleet workspace (tests/test_desk_trail_reads.py measured
#: them). A test workspace is too small for its own statistics to steer the
#: planner as a real one's do, so after its own ANALYZE these rows replace them.
FLEET_STATISTICS = {
    ("activity", "idx_activity_kind_id"): "65058 65058 1",
    ("activity", "idx_activity_actor"): "65058 9294 1",
    ("activity", "idx_activity_target"): "65058 65058 7 1",
    ("activity", "idx_activity_verb_window"): "65058 7229 276 276",
    ("issues", "idx_issues_status"): "10000 5000",
    ("issues", "idx_issues_archived"): "10000 371",
    ("issues", "idx_issues_project"): "10000 1429",
    ("issues", "idx_issues_assignee"): "10000 1429",
    # The idle sort's indexes (0096), measured on the same seeded workspace
    # after the migration: they too hold archived_at, so a statement that asks
    # for archived issues chooses between them and idx_issues_archived.
    ("issues", "idx_issues_idle"): "10000 304 154 1",
    ("issues", "idx_issues_closed_idle"): "10000 304 152 77 1",
}


def _statistics(path: Path, analyzed: bool) -> sqlite3.Connection:
    """Run ANALYZE when asked, as an operator might, put the fleet's statistics
    in place, and return a new connection, which plans from them."""
    if analyzed:
        conn = db.connect(path)
        try:
            conn.execute("ANALYZE")
            for (table, index), stat in FLEET_STATISTICS.items():
                conn.execute(
                    "DELETE FROM sqlite_stat1 WHERE tbl = ? AND idx = ?", (table, index)
                )
                conn.execute(
                    "INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (?, ?, ?)",
                    (table, index, stat),
                )
            conn.commit()
        finally:
            conn.close()
    return db.connect(path)


ANALYZED = pytest.mark.parametrize(
    "analyzed", [False, True], ids=["no-statistics", "analyzed"]
)


# --- the project brief's latest completions ------------------------------------


def _brief_workspace(conn):
    project = projects.create_project(
        conn, name="Brief", key="BRF", created_by=1, commit=True
    )
    other = projects.create_project(
        conn, name="Other", key="OTH", created_by=1, commit=True
    )
    for n in range(4):
        issue = _issue(conn, project["id"], f"work {n}")
        _event(conn, issue["id"], "commented")
        _complete(conn, issue["id"])
        _event(conn, issue["id"], "changed_status")
    return project, other


def _completion_statements(conn, project, actor):
    recording = Recording(conn)
    build_project_brief(recording, project_id=project["id"], actor=actor)
    found = [s for s in recording.statements if "claim_completed" in s[0]]
    assert len(found) == 2, recording.statements  # the count and the page
    return found


@ANALYZED
def test_the_briefs_latest_completion_seeks_each_issues_trail_not_every_event(
    ws, analyzed
):
    path, setup = ws
    project, _ = _brief_workspace(setup)
    conn = _statistics(path, analyzed)
    for who in (_user(conn, 1), _user(conn, 2), None):
        for sql, params in _completion_statements(conn, project, who):
            plan = _plan(conn, sql, params)
            trail = [s for s in plan if re.match(r"(SCAN|SEARCH) l?a\b", s)]
            # Not the whole issue trail newest first, grouped by issue: one seek
            # per issue of the project into that issue's own events, and the
            # event the seek chose read back by its key.
            assert not any("idx_activity_kind_id" in s for s in plan), plan
            assert not any(re.match(r"SCAN l?a\b", s) for s in plan), plan
            assert not any("TEMP B-TREE FOR GROUP BY" in s for s in plan), plan
            assert sorted(set(trail)) == [
                "SEARCH a USING INTEGER PRIMARY KEY (rowid=?)",
                "SEARCH la USING INDEX idx_activity_target "
                "(target_kind=? AND target_id=?)",
            ], plan
            # The project's issues come from their own index, not every issue,
            # and no issue is reached by a scan.
            assert plan[0] == "SEARCH i USING INDEX idx_issues_project (project_id=?)"
            assert not any(re.match(r"SCAN i\b", s) for s in plan), plan
    conn.close()


def test_the_briefs_work_does_not_grow_with_history_it_does_not_show(ws):
    """Events on other projects' issues, and an issue's history older than its
    newest completion, are never read: the brief takes exactly the same number
    of steps before and after a great deal of both is written."""
    _, conn = ws
    project, other = _brief_workspace(conn)
    member = _user(conn, 2)
    tracked = conn.execute(
        "SELECT id FROM issues WHERE project_id = ? ORDER BY id LIMIT 1",
        (project["id"],),
    ).fetchone()["id"]

    def brief():
        return build_project_brief(conn, project_id=project["id"], actor=member)

    before_packet = brief()
    before = _steps(conn, brief)
    elsewhere = [_issue(conn, other["id"], f"elsewhere {n}") for n in range(30)]
    for issue in elsewhere:
        for _ in range(6):
            _event(conn, issue["id"], "commented")
        _complete(conn, issue["id"])
    # A long trail on the tracked issue, all of it older than a newer completion.
    for _ in range(80):
        _event(conn, tracked, "commented")
    newest = _complete(conn, tracked)
    _event(conn, tracked, "changed_status")  # its trail ends as it did before
    after_packet = brief()
    after = _steps(conn, brief)
    # 320 events written, none of them read. What is left is the index of the
    # other project's 30 new issues bordering this project's range: about one
    # step for each of the brief's statements that walk this project's issues.
    assert before <= after <= before + 15, (before, after)
    completions = after_packet["latest_completions"]
    assert (
        completions["visible_total"]
        == before_packet["latest_completions"]["visible_total"]
    )
    assert completions["items"][0]["event_id"] == newest["id"]


# --- full-text search ------------------------------------------------------------


def _private(conn, name, key):
    project = projects.create_project(
        conn, name=name, key=key, created_by=1, commit=True
    )
    projects.set_visibility(conn, project["id"], "private", commit=True)
    return project


def _search_statements(conn, **kwargs):
    """The window's statement and the statement that reads the page's text."""
    recording = Recording(conn)
    assert search.search(recording, "lantern", **kwargs)
    found = [s for s in recording.statements if "search_index MATCH" in s[0]]
    assert len(found) == 2, recording.statements
    return found


@ANALYZED
def test_a_search_asks_each_hit_its_own_row_and_lists_no_source_table(ws, analyzed):
    path, setup = ws
    public = projects.create_project(
        setup, name="Open", key="OPN", created_by=1, commit=True
    )
    hidden = _private(setup, "Secret", "SEC")
    for project_id in (public["id"], hidden["id"], None):
        issue = _issue(setup, project_id, "lantern repair", "the lantern")
        comment_id = setup.execute(
            "INSERT INTO comments (issue_id, author_id, body) VALUES (?, 1, ?)",
            (issue["id"], "lantern comment"),
        ).lastrowid
        search.index_document(
            setup, kind="issue_comment", source_id=comment_id, commit=True
        )
    conn = _statistics(path, analyzed)
    sources = ("issues", "comments", "pages", "page_comments")
    aliases = ("gi", "gc", "gp", "ai", "ap", "ac", "i", "c", "pg")
    for actor in (_user(conn, 2), None):
        for kind in (None, "issue", "issue_comment"):
            (sql, params), text = _search_statements(conn, actor=actor, kind=kind)
            # The page's text is one walk of the matches between its lowest and
            # highest entry. In FTS5's index string ("M" the match, "<" and ">"
            # the rowid bounds), an "=" would be one search of the whole match
            # list per entry, which is what naming the entries as keys costs.
            (step,) = _plan(conn, *text)
            assert step.startswith("SCAN search_index VIRTUAL TABLE INDEX "), step
            strategy = step.rsplit(":", 1)[1]
            assert "<" in strategy and ">" in strategy and "=" not in strategy, step
            plan = _plan(conn, sql, params)
            touching = [
                s
                for s in plan
                if re.match(rf"(SCAN|SEARCH) ({'|'.join(sources + aliases)})\b", s)
            ]
            # Every source row is read by its key, one hit at a time: never a
            # list of every visible or archived issue, comment or page.
            assert touching, plan
            assert all("USING INTEGER PRIMARY KEY (rowid=?)" in s for s in touching), (
                actor,
                kind,
                plan,
            )
            assert not any(
                s.startswith("SCAN ")
                and "search_index" not in s
                and "subquery" not in s
                for s in plan
            ), plan
    conn.close()


def test_a_common_word_ranks_a_bounded_window_however_much_history_has_it(
    ws, monkeypatch
):
    """Once a word is in more entries than the window, a search for it takes
    about the same work whether it is in 4 times the window or 24 times it."""
    _, conn = ws
    monkeypatch.setattr(search, "RANK_WINDOW", 10)
    member = _user(conn, 2)
    public = projects.create_project(
        conn, name="Open", key="OPN", created_by=1, commit=True
    )

    def add(count):
        for n in range(count):
            _issue(conn, public["id"], f"lantern {n}", "a lantern in the body")

    def ask():
        return search.search(conn, "lantern", actor=member, limit=5)

    add(40)
    small = _steps(conn, ask)
    add(200)
    large = _steps(conn, ask)
    # Six times the matching history; the old read's work grew with it.
    assert large < small * 1.5, (small, large)


def test_the_window_is_the_newest_entries_the_caller_may_see(ws, monkeypatch):
    _, conn = ws
    member, outsider = _user(conn, 2), _user(conn, 3)
    monkeypatch.setattr(search, "RANK_WINDOW", 4)
    public = projects.create_project(
        conn, name="Open", key="OPN", created_by=1, commit=True
    )
    hidden = _private(conn, "Secret", "SEC")
    conn.execute(
        "INSERT INTO project_members (project_id, user_id) VALUES (?, ?)",
        (hidden["id"], member["id"]),
    )
    conn.commit()
    # Two old title matches, which rank above every body-only match.
    old = [_issue(conn, public["id"], "lantern lantern", "x")["id"] for _ in range(2)]
    body_only = [
        _issue(conn, public["id"], f"note {n}", "a lantern here")["id"]
        for n in range(4)
    ]
    archived = _issue(conn, public["id"], "lantern", "lantern")["id"]
    issues.set_archived(conn, archived, True, commit=True)
    assert len(old) == 2 and archived > body_only[-1]
    secret = [
        _issue(conn, hidden["id"], f"secret {n}", "a lantern there")["id"]
        for n in range(3)
    ]

    def ids(actor, **kwargs):
        return [
            h["source_id"]
            for h in search.search(conn, "lantern", actor=actor, **kwargs)
        ]

    # The outsider cannot see the secret issues, so they do not take the window's
    # places: the window is the four newest visible, unarchived matches, and the
    # old title matches fall outside it.
    assert ids(outsider) == body_only
    # Paging stops at the window's end.
    assert ids(outsider, limit=2, offset=2) == body_only[2:]
    assert ids(outsider, offset=4) == []
    # The member sees the secret issues, which are newer still.
    assert ids(member) == [*body_only[-1:], *secret]
    # A window as wide as the matches ranks exactly as an unbounded search: the
    # title matches first, then the rest in index order.
    monkeypatch.setattr(search, "RANK_WINDOW", 6)
    assert ids(outsider) == [*old, *body_only]


def test_the_shipped_window_is_stated_where_operators_read_it():
    assert search.RANK_WINDOW == 1_000
    performance = Path(__file__).resolve().parent.parent / "docs" / "PERFORMANCE.md"
    assert "RANK_WINDOW" in performance.read_text()
