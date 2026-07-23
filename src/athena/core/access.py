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


def is_project_member(conn: sqlite3.Connection, project_id: int, user_id: int) -> bool:
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
    conn: sqlite3.Connection,
    space_id: int,
    user_id: int,
    added_by: int | None,
    *,
    commit: bool = True,
) -> bool:
    """Grant a user read access to a (private) space. Idempotent, like its project
    twin; returns True only on a real change. ``commit=False`` composes this inside the
    audited space-access command's transaction."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO space_members (space_id, user_id, added_by) "
        "VALUES (?, ?, ?)",
        (space_id, user_id, added_by),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def remove_space_member(
    conn: sqlite3.Connection, space_id: int, user_id: int, *, commit: bool = True
) -> bool:
    """Revoke a user's space membership. Returns True if a row was removed.
    ``commit=False`` composes this inside the audited space-access command's
    transaction."""
    cur = conn.execute(
        "DELETE FROM space_members WHERE space_id = ? AND user_id = ?",
        (space_id, user_id),
    )
    if commit:
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


def can_see_space(conn: sqlite3.Connection, actor: dict | None, space_id: int) -> bool:
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


def container_write_reason(
    conn: sqlite3.Connection,
    actor: dict | None,
    *,
    kind: str,
    container_id: int,
    created_by: int,
) -> str | None:
    """The ONE rule for "may this actor EDIT or DELETE this project|space" — the gate
    whose four per-boundary copies had drifted until three of them leaked a hidden
    container's existence through a 403. Centralized here so the visibility-first
    ordering can't be gotten wrong (or forgotten) on one surface again.

    Returns None when the write is allowed, else a reason the caller maps to its own
    error shape:
      'not_visible' → the actor can't see the container → answer 404, so a hidden
                      container is indistinguishable from a missing one (no leak);
      'not_owner'   → visible, but the actor isn't its creator → 403.

    Visibility is ALWAYS checked first. Editing or deleting a whole container is
    CREATOR-ONLY: unlike toggling its visibility or managing its members — which an
    admin may also do on any container (a governance function) — destroying or
    renaming someone else's container is not an admin power. `kind` is 'project' or
    'space'; the caller has already fetched the row (and 404s a MISSING container
    itself), passing its created_by here."""
    if kind == "project":
        visible = can_see_project(conn, actor, container_id)
    elif kind == "space":
        visible = can_see_space(conn, actor, container_id)
    else:
        raise ValueError(f"unknown container kind: {kind!r}")
    if not visible:
        return "not_visible"
    if actor is not None and actor["id"] == created_by:
        return None
    return "not_owner"


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
    row = conn.execute("SELECT space_id FROM pages WHERE id = ?", (page_id,)).fetchone()
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


def _project_scope_keys(conn: sqlite3.Connection, project_ids: set[int]) -> set[str]:
    if not project_ids:
        return set()
    placeholders, params = _in_or_null(project_ids)
    return {
        row["activity_scope_key"]
        for row in conn.execute(
            f"SELECT activity_scope_key FROM projects WHERE id IN ({placeholders})",
            params,
        )
        if row["activity_scope_key"] is not None
    }


def _container_access_clause(
    alias: str,
    actor: dict | None,
    *,
    membership_table: str,
    container_id_column: str,
) -> tuple[str, list]:
    """SQL access predicate for one project/space alias in the caller's statement."""
    if actor is None:
        return f"{alias}.visibility = 'public'", []
    clause = (
        f"({alias}.visibility = 'public' OR {alias}.created_by = ? OR EXISTS ("
        f"SELECT 1 FROM {membership_table} membership "
        f"WHERE membership.{container_id_column} = {alias}.id "
        "AND membership.user_id = ?))"
    )
    return clause, [actor["id"], actor["id"]]


def project_visibility_clause(
    actor: dict | None, *, alias: str = "project"
) -> tuple[str, list]:
    """SQL predicate for resolving visible and missing projects identically."""
    if _is_admin(actor):
        return "1 = 1", []
    return _container_access_clause(
        alias,
        actor,
        membership_table="project_members",
        container_id_column="project_id",
    )


def _event_project_scope_clause(
    event_alias: str, actor: dict | None
) -> tuple[str, list]:
    project_access, params = _container_access_clause(
        "scope_project",
        actor,
        membership_table="project_members",
        container_id_column="project_id",
    )
    return (
        "NOT EXISTS (SELECT 1 FROM activity_visibility_projects avp "
        "LEFT JOIN projects scope_project "
        "ON scope_project.activity_scope_key = avp.project_scope_key "
        f"WHERE avp.event_id = {event_alias}.id "
        f"AND (scope_project.id IS NULL OR NOT ({project_access})))",
        params,
    )


def event_visibility_clause(
    conn: sqlite3.Connection, actor: dict | None, *, alias: str = "a"
) -> tuple[str, list]:
    """Return the shared, single-statement event visibility predicate.

    Every current-container and event-scope membership check is correlated inside the
    caller's SELECT/UPDATE. That makes authorization and data access one SQLite
    snapshot instead of materializing visible ids before a later statement.
    """
    if _is_admin(actor):
        return "", []

    current_project_access, current_project_params = _container_access_clause(
        "current_project",
        actor,
        membership_table="project_members",
        container_id_column="project_id",
    )
    issue_scope_access, issue_scope_params = _event_project_scope_clause(alias, actor)
    page_space_access, page_space_params = _container_access_clause(
        "page_space",
        actor,
        membership_table="space_members",
        container_id_column="space_id",
    )
    target_space_access, target_space_params = _container_access_clause(
        "target_space",
        actor,
        membership_table="space_members",
        container_id_column="space_id",
    )
    target_project_access, target_project_params = _container_access_clause(
        "target_project",
        actor,
        membership_table="project_members",
        container_id_column="project_id",
    )
    project_scope_access, project_scope_params = _event_project_scope_clause(
        alias, actor
    )

    clause = (
        f"(({alias}.target_kind = 'issue' "
        f"AND {alias}.visibility_restricted = 0 "
        f"AND EXISTS (SELECT 1 FROM issues i "
        f"WHERE i.id = {alias}.target_id "
        f"AND (i.project_id IS NULL OR EXISTS (SELECT 1 FROM projects current_project "
        f"WHERE current_project.id = i.project_id AND {current_project_access}))) "
        f"AND {issue_scope_access}) "
        f"OR ({alias}.target_kind = 'page' AND EXISTS ("
        f"SELECT 1 FROM pages pg JOIN spaces page_space "
        f"ON page_space.id = pg.space_id WHERE pg.id = {alias}.target_id "
        f"AND {page_space_access})) "
        f"OR ({alias}.target_kind = 'space' AND EXISTS ("
        f"SELECT 1 FROM spaces target_space "
        f"WHERE target_space.id = {alias}.target_id AND {target_space_access})) "
        f"OR ({alias}.target_kind = 'project' "
        f"AND {alias}.visibility_restricted = 0 "
        f"AND EXISTS (SELECT 1 FROM projects target_project "
        f"WHERE target_project.id = {alias}.target_id "
        f"AND {target_project_access}) "
        f"AND {project_scope_access}))"
    )
    params = [
        *current_project_params,
        *issue_scope_params,
        *page_space_params,
        *target_space_params,
        *target_project_params,
        *project_scope_params,
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
    scope_ph, scope_params = _in_or_null(_project_scope_keys(conn, visible_projects))
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
