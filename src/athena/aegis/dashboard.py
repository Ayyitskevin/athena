"""Read-only aggregations for the Aegis dashboard.

The dashboard is a thin client over these: all the counting SQL lives here (one
owner), so the web route just lays the numbers out. Everything counts only ACTIVE
issues — archived (soft-deleted) ones drop out of the dashboard the same way they
drop out of every list. No writes ever happen here.
"""
from __future__ import annotations

import sqlite3

from athena.aegis import issues, projects, sprints, statuses


def status_counts(conn: sqlite3.Connection) -> list[dict]:
    """How many active issues sit at each status, busiest first. Statuses are
    per-project free strings, so this groups by the raw name (a custom 'shipped'
    shows up as itself)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM issues "
        "WHERE archived_at IS NULL GROUP BY status ORDER BY count DESC, status"
    ).fetchall()
    return [dict(r) for r in rows]


def priority_counts(conn: sqlite3.Connection) -> list[dict]:
    """Active issues per priority, in the canonical low→urgent order (each priority
    is shown even at zero, so the shape of the backlog is always legible)."""
    counts = {
        r["priority"]: r["count"]
        for r in conn.execute(
            "SELECT priority, COUNT(*) AS count FROM issues "
            "WHERE archived_at IS NULL GROUP BY priority"
        ).fetchall()
    }
    return [{"priority": p, "count": counts.get(p, 0)} for p in issues.PRIORITIES]


def project_open_counts(conn: sqlite3.Connection) -> list[dict]:
    """Each project with its active-issue count, busiest first. A LEFT JOIN so a
    project with no issues still shows (at zero)."""
    rows = conn.execute(
        "SELECT p.id, p.name, p.key, "
        "COUNT(i.id) AS count "
        "FROM projects p "
        "LEFT JOIN issues i ON i.project_id = p.id AND i.archived_at IS NULL "
        "GROUP BY p.id ORDER BY count DESC, p.name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def backlog_count(conn: sqlite3.Connection) -> int:
    """Active issues in NO project (the backlog) — the row project_open_counts can't
    carry, since it groups by a real project id."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM issues "
        "WHERE project_id IS NULL AND archived_at IS NULL"
    ).fetchone()["n"]


def active_sprints(conn: sqlite3.Connection) -> list[dict]:
    """Every project's currently-active sprint, with its project name and how many
    ACTIVE issues it holds — the 'what's in flight right now' view. The count is
    active-only (archived_at IS NULL) so it agrees with every other number on the
    page; sprints.count_issues_in_sprint counts ALL rows on purpose (its job is the
    delete guard), so the dashboard does its own count here."""
    out: list[dict] = []
    for sprint in sprints.list_sprints(conn, state=sprints.ACTIVE):
        project = projects.get_project(conn, sprint["project_id"])
        issue_count = conn.execute(
            "SELECT COUNT(*) AS n FROM issues "
            "WHERE sprint_id = ? AND archived_at IS NULL",
            (sprint["id"],),
        ).fetchone()["n"]
        out.append(
            {
                **sprint,
                "project_name": project["name"] if project else None,
                "issue_count": issue_count,
            }
        )
    return out


def my_open_issues(
    conn: sqlite3.Connection, user_id: int, *, limit: int = 8
) -> list[dict]:
    """The signed-in user's plate: issues assigned to them that are still OPEN (not
    a done-category status) and not archived, oldest first, capped. 'Done' is the
    per-project closed category (statuses.is_done), so a project's custom closed
    state counts as done too."""
    assigned = issues.list_issues(conn, assignee_id=user_id)  # already non-archived
    open_ones = [
        issue
        for issue in assigned
        if not statuses.is_done(conn, issue["project_id"], issue["status"])
    ]
    return open_ones[:limit]


def totals(conn: sqlite3.Connection) -> dict:
    """Headline numbers: active issues, projects, and active sprints."""
    active_issues = conn.execute(
        "SELECT COUNT(*) AS n FROM issues WHERE archived_at IS NULL"
    ).fetchone()["n"]
    project_count = conn.execute(
        "SELECT COUNT(*) AS n FROM projects"
    ).fetchone()["n"]
    active_sprint_count = conn.execute(
        "SELECT COUNT(*) AS n FROM sprints WHERE state = ?", (sprints.ACTIVE,)
    ).fetchone()["n"]
    return {
        "active_issues": active_issues,
        "projects": project_count,
        "active_sprints": active_sprint_count,
    }
