"""Who may see a private project or space — the one place that answers visibility.

Projects and spaces are PUBLIC by default: anyone, signed in or not, may read them,
exactly as Athena has always behaved. Marking one PRIVATE narrows reads to its
members, its creator, and admins. Membership lives in the project_members /
space_members join tables (the read-side twins of aegis/issue_contributors), and
this module owns those tables AND the resolver that every list/detail/search path
filters through.

This module lives in **core** on purpose: the widest-blast-radius reads — global
search, the activity feed, the notifications inbox, the label hub, cross-link
backlinks — are all in core and cross both Aegis and Mentor, so the one resolver they
share has to live below both. It READS the `visibility` column off projects/spaces
with raw SQL (the same cross-module read core/labels.py does over issue/page rows) but
never WRITES it — flipping a project/space private stays with its owning module
(aegis.projects / mentor.spaces), so there is still exactly one writer per table.

Nothing here gates anything on its own: callers ask `visible_project_ids` /
`can_see_project` (and the space twins) and filter their own reads. Because every
project/space defaults to public, those answers are "everything" until a row is
actually marked private — so wiring this resolver into the read paths is INERT until
the privacy feature is switched on in a later slice.
"""
from __future__ import annotations

from collections.abc import Collection
import sqlite3

from athena.core import users

# An admin sees everything (the god view that keeps the model simple); the creator of
# a private container is always let in (so nobody can lock themselves out of their own
# project); and an explicit member is let in. Everyone else sees only public rows. We
# inline the admin check against the role rather than import core.identity so this
# bottom-of-the-stack module stays dependency-light.


def _is_admin(actor: dict | None) -> bool:
    return actor is not None and actor.get("role") == users.ADMIN_ROLE


# --- project membership -----------------------------------------------------

def add_project_member(
    conn: sqlite3.Connection, project_id: int, user_id: int, added_by: int | None
) -> bool:
    """Grant a user read access to a (private) project. Idempotent: re-adding the same
    pair is a no-op (composite PK + OR IGNORE), returning False so the caller records
    an audit event only on a real change. Raises sqlite3.IntegrityError if the project
    or user doesn't exist (the foreign keys refuse the orphan)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO project_members (project_id, user_id, added_by) "
        "VALUES (?, ?, ?)",
        (project_id, user_id, added_by),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_project_member(
    conn: sqlite3.Connection, project_id: int, user_id: int
) -> bool:
    """Revoke a user's membership. Returns True if a row was removed, False if the
    user wasn't a member (so the caller can 404)."""
    cur = conn.execute(
        "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def is_project_member(
    conn: sqlite3.Connection, project_id: int, user_id: int
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
        is not None
    )


def list_project_members(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """Everyone explicitly granted access to this project, with their display name and
    who/when granted — for the manage-members view. Does NOT include the creator or
    admins (who get in implicitly); it lists only the membership rows."""
    rows = conn.execute(
        "SELECT pm.user_id, u.name AS name, u.is_agent AS is_agent, "
        "pm.added_by, pm.added_at "
        "FROM project_members pm JOIN users u ON u.id = pm.user_id "
        "WHERE pm.project_id = ? ORDER BY u.name COLLATE NOCASE",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- space membership -------------------------------------------------------

def add_space_member(
    conn: sqlite3.Connection, space_id: int, user_id: int, added_by: int | None
) -> bool:
    """Grant a user read access to a (private) space. Idempotent, like its project
    twin; returns True only on a real change."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO space_members (space_id, user_id, added_by) "
        "VALUES (?, ?, ?)",
        (space_id, user_id, added_by),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_space_member(
    conn: sqlite3.Connection, space_id: int, user_id: int
) -> bool:
    """Revoke a user's space membership. Returns True if a row was removed."""
    cur = conn.execute(
        "DELETE FROM space_members WHERE space_id = ? AND user_id = ?",
        (space_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def is_space_member(conn: sqlite3.Connection, space_id: int, user_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM space_members WHERE space_id = ? AND user_id = ?",
            (space_id, user_id),
        ).fetchone()
        is not None
    )


def list_space_members(conn: sqlite3.Connection, space_id: int) -> list[dict]:
    """Everyone explicitly granted access to this space (excludes creator/admins)."""
    rows = conn.execute(
        "SELECT sm.user_id, u.name AS name, u.is_agent AS is_agent, "
        "sm.added_by, sm.added_at "
        "FROM space_members sm JOIN users u ON u.id = sm.user_id "
        "WHERE sm.space_id = ? ORDER BY u.name COLLATE NOCASE",
        (space_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- the read-side resolver -------------------------------------------------

def can_see_project(
    conn: sqlite3.Connection, actor: dict | None, project_id: int
) -> bool:
    """May this actor read this project (and its issues)? Public → always. Private →
    only an admin, the creator, or a member. A missing project is False (the caller
    turns that into its own 404). `actor` is the resolved user dict, or None for an
    anonymous/signed-out viewer."""
    row = conn.execute(
        "SELECT visibility, created_by FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return False
    if row["visibility"] == "public":
        return True
    if actor is None:
        return False
    if _is_admin(actor) or actor["id"] == row["created_by"]:
        return True
    return is_project_member(conn, project_id, actor["id"])


def can_see_space(
    conn: sqlite3.Connection, actor: dict | None, space_id: int
) -> bool:
    """May this actor read this space (and its pages)? Same rule as can_see_project."""
    row = conn.execute(
        "SELECT visibility, created_by FROM spaces WHERE id = ?", (space_id,)
    ).fetchone()
    if row is None:
        return False
    if row["visibility"] == "public":
        return True
    if actor is None:
        return False
    if _is_admin(actor) or actor["id"] == row["created_by"]:
        return True
    return is_space_member(conn, space_id, actor["id"])


def visible_project_ids(conn: sqlite3.Connection, actor: dict | None) -> set[int]:
    """The set of project ids this actor may read — the filter every issue/project
    LIST applies. An admin gets every project; anyone else gets the public ones plus
    the private ones they created or are a member of; an anonymous viewer gets only
    the public ones. (Backlog issues have no project and are gated separately by the
    issue reads, not here.)"""
    if _is_admin(actor):
        return {r["id"] for r in conn.execute("SELECT id FROM projects")}
    visible = {
        r["id"]
        for r in conn.execute("SELECT id FROM projects WHERE visibility = 'public'")
    }
    if actor is not None:
        visible |= {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM projects WHERE visibility = 'private' AND created_by = ?",
                (actor["id"],),
            )
        }
        visible |= {
            r["project_id"]
            for r in conn.execute(
                "SELECT project_id FROM project_members WHERE user_id = ?",
                (actor["id"],),
            )
        }
    return visible


def can_see_issue(conn: sqlite3.Connection, actor: dict | None, issue_id: int) -> bool:
    """Whether the actor may read this issue, resolved by its id — the cross-link
    gate. Looks up the issue's project and defers to can_see_project_or_backlog. A
    missing issue is not visible (a deleted target has no container to authorize
    against — the safe default the activity gate uses too)."""
    row = conn.execute(
        "SELECT project_id FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    if row is None:
        return False
    return can_see_project_or_backlog(conn, actor, row["project_id"])


def can_see_page(conn: sqlite3.Connection, actor: dict | None, page_id: int) -> bool:
    """Whether the actor may read this page, resolved by its id — the page twin of
    can_see_issue. Looks up the page's space and defers to can_see_space; a missing
    page is not visible."""
    row = conn.execute(
        "SELECT space_id FROM pages WHERE id = ?", (page_id,)
    ).fetchone()
    if row is None:
        return False
    return can_see_space(conn, actor, row["space_id"])


def visible_project_filter(
    conn: sqlite3.Connection, actor: dict | None
) -> set[int] | None:
    """The project-id set an issue LIST should be constrained to — or None when the
    actor may see every project (an admin), in which case no filtering is needed.
    None ("sees everything") is deliberately distinct from an empty set ("sees no
    project → only the backlog"). This is what callers hand to issues.list_issues so
    it can skip the IN-clause entirely for admins rather than list every project id."""
    if _is_admin(actor):
        return None
    return visible_project_ids(conn, actor)


def can_see_project_or_backlog(
    conn: sqlite3.Connection, actor: dict | None, project_id: int | None
) -> bool:
    """Whether the actor may read an issue in this project scope — the single-issue
    gate (detail pages). A None project is the shared backlog: it has no container to
    gate on, so it reads like a public project (visible to everyone, signed in or
    not). Otherwise defer to can_see_project."""
    if project_id is None:
        return True
    return can_see_project(conn, actor, project_id)


def visible_space_filter(
    conn: sqlite3.Connection, actor: dict | None
) -> set[int] | None:
    """The space twin of visible_project_filter: the space-id set a page/space LIST
    should be constrained to, or None when the actor sees every space (an admin). None
    ("sees everything") is distinct from an empty set ("sees no space"). Pages always
    belong to a space (no backlog), so unlike issues there is no nullable case."""
    if _is_admin(actor):
        return None
    return visible_space_ids(conn, actor)


def visible_space_ids(conn: sqlite3.Connection, actor: dict | None) -> set[int]:
    """The set of space ids this actor may read — the filter every page/space LIST
    applies. Same rule as visible_project_ids."""
    if _is_admin(actor):
        return {r["id"] for r in conn.execute("SELECT id FROM spaces")}
    visible = {
        r["id"]
        for r in conn.execute("SELECT id FROM spaces WHERE visibility = 'public'")
    }
    if actor is not None:
        visible |= {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM spaces WHERE visibility = 'private' AND created_by = ?",
                (actor["id"],),
            )
        }
        visible |= {
            r["space_id"]
            for r in conn.execute(
                "SELECT space_id FROM space_members WHERE user_id = ?",
                (actor["id"],),
            )
        }
    return visible


def _in_or_null(ids: Collection[int | str]) -> tuple[str, list]:
    """Comma-joined placeholders + params for an IN list, or the literal token 'NULL'
    (which matches nothing) when the set is empty — SQLite rejects 'IN ()'. Sorted for
    a deterministic query string."""
    vals = sorted(ids)
    return (",".join("?" for _ in vals), vals) if vals else ("NULL", [])


def _project_scope_keys(
    conn: sqlite3.Connection, project_ids: set[int]
) -> set[str]:
    if not project_ids:
        return set()
    placeholders, params = _in_or_null(project_ids)
    return {
        row["activity_scope_key"]
        for row in conn.execute(
            "SELECT activity_scope_key FROM projects "
            f"WHERE id IN ({placeholders})",
            params,
        )
        if row["activity_scope_key"] is not None
    }


def event_visibility_clause(
    conn: sqlite3.Connection, actor: dict | None, *, alias: str = "a"
) -> tuple[str, list]:
    """Return the shared SQL visibility predicate for activity and notifications.

    An issue event is visible only when the actor can see the issue in its current
    container, the row has trustworthy event-time scope, and the actor can see EVERY
    project attached to that event. Requiring all scopes preserves privacy across
    repeated moves (A → B → C); current-project gating alone would re-publish history.
    Other target kinds retain their existing current-container rules. Admins are the
    ungated forensic view, including legacy/imported issue rows that fail closed.
    """
    if _is_admin(actor):
        return "", []
    visible_projects = visible_project_ids(conn, actor)
    proj_ph, proj_params = _in_or_null(visible_projects)
    scope_ph, scope_params = _in_or_null(
        _project_scope_keys(conn, visible_projects)
    )
    space_ph, space_params = _in_or_null(visible_space_ids(conn, actor))
    clause = (
        f"(({alias}.target_kind = 'issue' "
        f"AND {alias}.visibility_restricted = 0 "
        f"AND EXISTS (SELECT 1 FROM issues i "
        f"WHERE i.id = {alias}.target_id "
        f"AND (i.project_id IS NULL OR i.project_id IN ({proj_ph}))) "
        f"AND (SELECT COUNT(*) FROM activity_visibility_projects avp "
        f"WHERE avp.event_id = {alias}.id) = "
        f"(SELECT COUNT(*) FROM activity_visibility_projects avp "
        f"WHERE avp.event_id = {alias}.id "
        f"AND avp.project_scope_key IN ({scope_ph}))) "
        f"OR ({alias}.target_kind = 'page' AND EXISTS ("
        f"SELECT 1 FROM pages pg WHERE pg.id = {alias}.target_id "
        f"AND pg.space_id IN ({space_ph}))) "
        f"OR ({alias}.target_kind = 'space' AND {alias}.target_id IN ({space_ph})) "
        f"OR ({alias}.target_kind = 'project' "
        f"AND {alias}.visibility_restricted = 0 "
        f"AND {alias}.target_id IN ({proj_ph}) "
        f"AND (SELECT COUNT(*) FROM activity_visibility_projects avp "
        f"WHERE avp.event_id = {alias}.id) = "
        f"(SELECT COUNT(*) FROM activity_visibility_projects avp "
        f"WHERE avp.event_id = {alias}.id "
        f"AND avp.project_scope_key IN ({scope_ph}))))"
    )
    params = [
        *proj_params,
        *scope_params,
        *space_params,
        *space_params,
        *proj_params,
        *scope_params,
    ]
    return clause, params


def can_see_complete_issue_history(
    conn: sqlite3.Connection,
    actor: dict | None,
    issue_id: int,
) -> bool:
    """Whether `actor` may read every event needed for exact issue time-travel.

    The projection may use a later transition's `before` value to recover creation
    state, so access to an arbitrary cutoff still requires the whole timeline. Folding
    a filtered subset would silently invent a false history. Legacy/imported events
    are restricted; multi-project events require visibility to every scope.
    """
    if _is_admin(actor):
        return True
    row = conn.execute(
        "SELECT project_id FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    if row is None or not can_see_project_or_backlog(conn, actor, row["project_id"]):
        return False

    visible_projects = visible_project_ids(conn, actor)
    scope_ph, scope_params = _in_or_null(
        _project_scope_keys(conn, visible_projects)
    )
    hidden = conn.execute(
        "SELECT 1 FROM activity a "
        "WHERE a.target_kind = 'issue' AND a.target_id = ? "
        "AND (a.visibility_restricted = 1 OR "
        "(SELECT COUNT(*) FROM activity_visibility_projects avp "
        "WHERE avp.event_id = a.id) <> "
        "(SELECT COUNT(*) FROM activity_visibility_projects avp "
        "WHERE avp.event_id = a.id "
        f"AND avp.project_scope_key IN ({scope_ph}))) LIMIT 1",
        (issue_id, *scope_params),
    ).fetchone()
    return hidden is None
