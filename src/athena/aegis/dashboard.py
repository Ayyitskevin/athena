"""Read-only aggregations for the Aegis dashboard.

The dashboard is a thin client over these: all the counting SQL lives here (one
owner), so the web route just lays the numbers out. Everything counts only ACTIVE
issues — archived (soft-deleted) ones drop out of the dashboard the same way they
drop out of every list. No writes ever happen here.

Every aggregation also respects project visibility: each takes a visible_project_ids
set (what the viewer may see), and the numbers count only issues/projects/sprints in
it. None means "no gating" — an admin (who sees everything) or an internal caller —
and is distinct from an empty set ("sees no project", so only the backlog counts).
The backlog (issues with no project) has nothing to gate on, so it is always counted.
"""
from __future__ import annotations

import sqlite3

from athena.aegis import issues, projects, sprints, statuses


def _issue_scope(visible_project_ids: set[int] | None) -> tuple[str, list]:
    """The visibility predicate for the (unaliased) issues table: an SQL fragment with
    no leading AND, plus its params. None → ('', []) (no gating). A set keeps the
    backlog (project_id IS NULL) and the visible projects; an empty set narrows to the
    backlog alone."""
    if visible_project_ids is None:
        return "", []
    if not visible_project_ids:
        return "project_id IS NULL", []
    placeholders = ",".join("?" for _ in visible_project_ids)
    return (
        f"(project_id IS NULL OR project_id IN ({placeholders}))",
        list(visible_project_ids),
    )


def status_counts(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> list[dict]:
    """How many active issues sit at each status, busiest first. Statuses are
    per-project free strings, so this groups by the raw name (a custom 'shipped'
    shows up as itself)."""
    scope, params = _issue_scope(visible_project_ids)
    where = "WHERE archived_at IS NULL" + (f" AND {scope}" if scope else "")
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS count FROM issues {where} "
        "GROUP BY status ORDER BY count DESC, status",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def priority_counts(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> list[dict]:
    """Active issues per priority, in the canonical low→urgent order (each priority
    is shown even at zero, so the shape of the backlog is always legible)."""
    scope, params = _issue_scope(visible_project_ids)
    where = "WHERE archived_at IS NULL" + (f" AND {scope}" if scope else "")
    counts = {
        r["priority"]: r["count"]
        for r in conn.execute(
            f"SELECT priority, COUNT(*) AS count FROM issues {where} GROUP BY priority",
            params,
        ).fetchall()
    }
    return [{"priority": p, "count": counts.get(p, 0)} for p in issues.PRIORITIES]


def project_open_counts(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> list[dict]:
    """Each project the viewer may see with its active-issue count, busiest first. A
    LEFT JOIN so a project with no issues still shows (at zero). A None filter shows
    every project (admin); an empty set shows none."""
    if visible_project_ids is None:
        proj_where, params = "", []
    elif not visible_project_ids:
        return []
    else:
        placeholders = ",".join("?" for _ in visible_project_ids)
        proj_where = f"WHERE p.id IN ({placeholders})"
        params = list(visible_project_ids)
    rows = conn.execute(
        "SELECT p.id, p.name, p.key, "
        "COUNT(i.id) AS count "
        "FROM projects p "
        "LEFT JOIN issues i ON i.project_id = p.id AND i.archived_at IS NULL "
        f"{proj_where} "
        "GROUP BY p.id ORDER BY count DESC, p.name COLLATE NOCASE",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def backlog_count(conn: sqlite3.Connection) -> int:
    """Active issues in NO project (the backlog) — the row project_open_counts can't
    carry, since it groups by a real project id. The backlog has no project to gate
    on, so it is visible to everyone; no visibility filter applies."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM issues "
        "WHERE project_id IS NULL AND archived_at IS NULL"
    ).fetchone()["n"]


def active_sprints(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> list[dict]:
    """Every visible project's currently-active sprint, with its project name and how
    many ACTIVE issues it holds — the 'what's in flight right now' view. The count is
    active-only (archived_at IS NULL) so it agrees with every other number on the
    page; sprints.count_issues_in_sprint counts ALL rows on purpose (its job is the
    delete guard), so the dashboard does its own count here. A sprint in a project the
    viewer can't see is dropped; its issues are all in that project, so once the sprint
    is shown its whole count is visible too.

    Also returns open_count / done_count (done = category 'done') so the UI can show
    a simple progress split without a reporting engine.
    """
    out: list[dict] = []
    for sprint in sprints.list_sprints(conn, state=sprints.ACTIVE):
        if visible_project_ids is not None and sprint["project_id"] not in visible_project_ids:
            continue
        project = projects.get_project(conn, sprint["project_id"])
        rows = conn.execute(
            "SELECT id, status, project_id FROM issues "
            "WHERE sprint_id = ? AND archived_at IS NULL",
            (sprint["id"],),
        ).fetchall()
        open_count = 0
        done_count = 0
        for row in rows:
            if statuses.is_done(conn, row["project_id"], row["status"]):
                done_count += 1
            else:
                open_count += 1
        issue_count = open_count + done_count
        out.append(
            {
                **sprint,
                "project_name": project["name"] if project else None,
                "issue_count": issue_count,
                "open_count": open_count,
                "done_count": done_count,
            }
        )
    return out


def my_open_issues(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    limit: int = 8,
    visible_project_ids: set[int] | None = None,
) -> list[dict]:
    """The signed-in user's plate: issues assigned to them that are still OPEN (not
    a done-category status) and not archived, oldest first, capped. 'Done' is the
    per-project closed category (statuses.is_done), so a project's custom closed
    state counts as done too. Gated by visibility too, so an issue assigned to the
    user in a project they can no longer see doesn't surface."""
    assigned = issues.list_issues(
        conn, assignee_id=user_id, visible_project_ids=visible_project_ids
    )  # already non-archived
    open_ones = [
        issue
        for issue in assigned
        if not statuses.is_done(conn, issue["project_id"], issue["status"])
    ]
    return open_ones[:limit]


def totals(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> dict:
    """Headline numbers: active issues, projects, and active sprints — each counted
    only over what the viewer may see. Also open/done/doing splits and agent
    delegation load (open issues with at least one agent contributor)."""
    scope, params = _issue_scope(visible_project_ids)
    where = "WHERE archived_at IS NULL" + (f" AND {scope}" if scope else "")
    active_rows = conn.execute(
        f"SELECT id, status, project_id FROM issues {where}", params
    ).fetchall()
    active_issues = len(active_rows)
    open_issues = 0
    doing_issues = 0
    done_issues = 0
    for row in active_rows:
        cat = statuses.category_of(conn, row["project_id"], row["status"]) or "todo"
        if cat == "done":
            done_issues += 1
        elif cat == "doing":
            doing_issues += 1
        else:
            open_issues += 1

    if visible_project_ids is None:
        project_count = conn.execute(
            "SELECT COUNT(*) AS n FROM projects"
        ).fetchone()["n"]
        active_sprint_count = conn.execute(
            "SELECT COUNT(*) AS n FROM sprints WHERE state = ?", (sprints.ACTIVE,)
        ).fetchone()["n"]
    elif not visible_project_ids:
        project_count = 0
        active_sprint_count = 0
    else:
        placeholders = ",".join("?" for _ in visible_project_ids)
        vis = list(visible_project_ids)
        project_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM projects WHERE id IN ({placeholders})", vis
        ).fetchone()["n"]
        active_sprint_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM sprints "
            f"WHERE state = ? AND project_id IN ({placeholders})",
            [sprints.ACTIVE, *vis],
        ).fetchone()["n"]

    delegated = delegated_to_agents(conn, visible_project_ids=visible_project_ids, limit=10_000)
    return {
        "active_issues": active_issues,
        "open_issues": open_issues,
        "doing_issues": doing_issues,
        "done_issues": done_issues,
        "projects": project_count,
        "active_sprints": active_sprint_count,
        "delegated_to_agents": len(delegated),
    }


def category_counts(
    conn: sqlite3.Connection, visible_project_ids: set[int] | None = None
) -> list[dict]:
    """Active issues grouped by lifecycle category (todo / doing / done).

    Status *names* are free strings per project; categories are the stable shape
    the dashboard uses for a glanceable open/in-flight/closed split.
    """
    scope, params = _issue_scope(visible_project_ids)
    where = "WHERE archived_at IS NULL" + (f" AND {scope}" if scope else "")
    rows = conn.execute(
        f"SELECT status, project_id FROM issues {where}", params
    ).fetchall()
    tallies = {"todo": 0, "doing": 0, "done": 0}
    for row in rows:
        cat = statuses.category_of(conn, row["project_id"], row["status"]) or "todo"
        if cat not in tallies:
            cat = "doing"
        tallies[cat] += 1
    return [{"category": key, "count": tallies[key]} for key in ("todo", "doing", "done")]


def delegated_to_agents(
    conn: sqlite3.Connection,
    *,
    visible_project_ids: set[int] | None = None,
    limit: int = 8,
) -> list[dict]:
    """Open (non-done) active issues that have at least one agent contributor.

    Primary assignee stays human-accountable; agents show up as teammates on the
    issue. Visibility-gated like every other dashboard number.
    """
    scope, params = _issue_scope(visible_project_ids)
    where = "i.archived_at IS NULL" + (f" AND {scope.replace('project_id', 'i.project_id')}" if scope else "")
    # Rewrite bare project_id references in scope for the aliased issues table.
    if scope:
        where = "i.archived_at IS NULL AND " + scope.replace(
            "project_id", "i.project_id"
        )
    rows = conn.execute(
        "SELECT i.id, i.title, i.status, i.priority, i.project_id, i.project_seq, "
        "i.assignee_id, p.key AS project_key, p.name AS project_name, "
        "ua.name AS assignee_name, "
        "GROUP_CONCAT(u.name, ', ') AS agent_names, "
        "COUNT(u.id) AS agent_count "
        "FROM issues i "
        "JOIN issue_contributors ic ON ic.issue_id = i.id "
        "JOIN users u ON u.id = ic.user_id AND u.is_agent = 1 "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN users ua ON ua.id = i.assignee_id "
        f"WHERE {where} "
        "GROUP BY i.id "
        "ORDER BY i.id ASC",
        params,
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if statuses.is_done(conn, row["project_id"], row["status"]):
            continue
        item = dict(row)
        if item.get("project_key") and item.get("project_seq") is not None:
            item["key"] = f"{item['project_key']}-{item['project_seq']}"
        else:
            item["key"] = None
        out.append(item)
        if len(out) >= limit:
            break
    return out


def agent_loads(
    conn: sqlite3.Connection,
    *,
    visible_project_ids: set[int] | None = None,
    limit: int = 8,
) -> list[dict]:
    """Per-agent open contribution load (non-done issues they contribute to)."""
    scope, params = _issue_scope(visible_project_ids)
    if scope:
        where = "i.archived_at IS NULL AND " + scope.replace(
            "project_id", "i.project_id"
        )
    else:
        where = "i.archived_at IS NULL"
    rows = conn.execute(
        "SELECT u.id AS agent_id, u.name AS agent_name, u.email AS agent_email, "
        "i.id AS issue_id, i.status, i.project_id "
        "FROM users u "
        "JOIN issue_contributors ic ON ic.user_id = u.id "
        "JOIN issues i ON i.id = ic.issue_id "
        f"WHERE u.is_agent = 1 AND {where} "
        "ORDER BY u.name COLLATE NOCASE, i.id",
        params,
    ).fetchall()
    loads: dict[int, dict] = {}
    for row in rows:
        if statuses.is_done(conn, row["project_id"], row["status"]):
            continue
        bucket = loads.setdefault(
            row["agent_id"],
            {
                "agent_id": row["agent_id"],
                "agent_name": row["agent_name"],
                "agent_email": row["agent_email"],
                "open_count": 0,
            },
        )
        bucket["open_count"] += 1
    ordered = sorted(
        loads.values(),
        key=lambda r: (-r["open_count"], r["agent_name"].casefold()),
    )
    return ordered[:limit]


def snapshot(
    conn: sqlite3.Connection,
    *,
    visible_project_ids: set[int] | None = None,
    user_id: int | None = None,
) -> dict:
    """Full dashboard payload for web + JSON API (one owner, one shape)."""
    return {
        "totals": totals(conn, visible_project_ids),
        "category_counts": category_counts(conn, visible_project_ids),
        "status_counts": status_counts(conn, visible_project_ids),
        "priority_counts": priority_counts(conn, visible_project_ids),
        "projects": project_open_counts(conn, visible_project_ids),
        "backlog_count": backlog_count(conn),
        "active_sprints": active_sprints(conn, visible_project_ids),
        "delegated_to_agents": delegated_to_agents(
            conn, visible_project_ids=visible_project_ids
        ),
        "agent_loads": agent_loads(conn, visible_project_ids=visible_project_ids),
        "my_issues": (
            my_open_issues(conn, user_id, visible_project_ids=visible_project_ids)
            if user_id is not None
            else []
        ),
    }
