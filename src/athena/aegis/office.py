"""The Office: one chair per agent.

The desk is the whole board. The office is the cubicle — the unique
Athena idea that an agent sits in at most one chair (one active lease),
works only the fenced paths, and stands up with complete_claim.

It composes leases + issues + the declared seat slug. It does not lock,
spawn a process, or invent a git remote.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3

from athena.aegis import dependencies, issues, leases, projects, statuses
from athena.core import access, fleet_roster, users

FLOOR_SCHEMA = "athena.project_floor.v1"

SCHEMA = "athena.office.v1"

PROTOCOL = {
    "claim_one_issue": True,
    "do_not_touch_other_work": True,
    "complete_does_not_close_issue": True,
    "meaning": (
        "Sit in at most one chair. Claim that issue. Fence files with "
        "paths. complete_claim stands you up; it does not close the issue."
    ),
}


def checkout_hint(
    *, issue_key: str | None, issue_id: int, seat_slug: str | None
) -> str:
    """Branch name only — Athena does not know your remotes."""
    key = (issue_key or f"issue-{issue_id}").lower().replace(" ", "-")
    if seat_slug:
        return f"athena/{key}-{seat_slug}"
    return f"athena/{key}"


def _chair_from_lease(
    conn: sqlite3.Connection,
    lease: dict,
    seat_slug: str | None,
    *,
    actor: dict | None,
) -> dict:
    issue = issues.get_issue(conn, int(lease["issue_id"]))
    key = None if issue is None else issue.get("key")
    issue_id = int(lease["issue_id"])
    blockers = (
        [] if issue is None else dependencies.open_blockers(conn, issue_id, actor=actor)
    )
    return {
        "issue_id": issue_id,
        "issue_key": key,
        "issue_title": None if issue is None else issue.get("title"),
        "project_id": None if issue is None else issue.get("project_id"),
        "generation": lease.get("generation"),
        "declared_paths": list(lease.get("declared_paths") or []),
        "expires_at": lease.get("expires_at"),
        "blocked_by": [
            {
                "id": row["id"],
                "key": row.get("key"),
                "title": row.get("title"),
                "status": row.get("status"),
            }
            for row in blockers
        ],
        "checkout_hint": checkout_hint(
            issue_key=key, issue_id=issue_id, seat_slug=seat_slug
        ),
    }


def build_office(
    conn: sqlite3.Connection,
    *,
    actor: dict,
    now: datetime | None = None,
    inbox_items: list[dict] | None = None,
) -> dict:
    """This actor's cubicle. ``inbox_items`` avoids a second delegation read
    when the desk already has them."""
    seat_slug = fleet_roster.seat_slug_for_email(actor.get("email"))
    held = leases.leases_held_by(conn, holder_id=int(actor["id"]), limit=20, now=now)
    active = [row for row in held if row.get("active")]
    warnings: list[str] = []
    chair = None
    if len(active) == 1:
        chair = _chair_from_lease(conn, active[0], seat_slug, actor=actor)
    elif len(active) > 1:
        warnings.append(
            "more than one active lease; sit in one chair and release the others"
        )
        chair = _chair_from_lease(conn, active[0], seat_slug, actor=actor)

    next_to_sit = None
    if chair is None and inbox_items:
        first = inbox_items[0].get("issue") or {}
        if first.get("id") is not None:
            next_to_sit = {
                "issue_id": first["id"],
                "issue_key": first.get("key"),
                "issue_title": first.get("title"),
                "issue_etag": inbox_items[0].get("issue_etag"),
            }

    return {
        "schema": SCHEMA,
        "seat_slug": seat_slug,
        "seated": chair is not None and len(active) == 1,
        "chair": chair,
        "next_to_sit": next_to_sit,
        "active_lease_count": len(active),
        "protocol": PROTOCOL,
        "warnings": warnings,
        "semantics": {
            "snapshot": "this_actor_right_now",
            "does_not_assert": ["alive", "running", "repo_exists", "branch_exists"],
        },
    }


def build_occupancy(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[dict]:
    """Who is sitting where — for the operator cockpit. Not a liveness claim."""
    rows = []
    for lease in leases.list_active_leases(conn):
        holder = users.get_user(conn, int(lease["holder_id"]))
        email = None if holder is None else holder.get("email")
        slug = fleet_roster.seat_slug_for_email(email)
        issue = issues.get_issue(conn, int(lease["issue_id"]))
        rows.append(
            {
                "seat_slug": slug,
                "holder_id": lease["holder_id"],
                "holder_name": lease.get("holder_name"),
                "issue_id": lease["issue_id"],
                "issue_key": None if issue is None else issue.get("key"),
                "issue_title": None if issue is None else issue.get("title"),
                "declared_paths": list(lease.get("declared_paths") or []),
                "expires_at": lease.get("expires_at"),
            }
        )
    rows.sort(key=lambda row: (row["seat_slug"] or "zzz", row["issue_id"]))
    return rows


def build_floor(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    actor: dict | None,
    now: datetime | None = None,
) -> dict | None:
    """One project as a floor of chairs. Missing or hidden project → None."""
    project = projects.get_project(conn, project_id)
    if project is None or not access.can_see_project(conn, actor, project_id):
        return None

    open_issues = [
        issue
        for issue in issues.list_issues(
            conn, project_id=project_id, include_archived=False
        )
        if not statuses.is_done(conn, issue.get("project_id"), issue["status"])
    ]
    lease_by_issue = {
        int(lease["issue_id"]): lease for lease in leases.list_active_leases(conn)
    }

    chairs: list[dict] = []
    for issue in open_issues:
        issue_id = int(issue["id"])
        lease = lease_by_issue.get(issue_id)
        occupant = None
        if lease is not None:
            holder = users.get_user(conn, int(lease["holder_id"]))
            occupant = {
                "holder_id": lease["holder_id"],
                "holder_name": lease.get("holder_name"),
                "seat_slug": fleet_roster.seat_slug_for_email(
                    None if holder is None else holder.get("email")
                ),
                "declared_paths": list(lease.get("declared_paths") or []),
                "expires_at": lease.get("expires_at"),
                "generation": lease.get("generation"),
            }
        assignee_email = None
        if issue.get("assignee_id") is not None:
            assignee = users.get_user(conn, int(issue["assignee_id"]))
            assignee_email = None if assignee is None else assignee.get("email")
        blockers = dependencies.open_blockers(conn, issue_id, actor=actor)
        chairs.append(
            {
                "issue_id": issue_id,
                "issue_key": issue.get("key"),
                "issue_title": issue.get("title"),
                "status": issue.get("status"),
                "priority": issue.get("priority"),
                "assignee_id": issue.get("assignee_id"),
                "assignee_name": issue.get("assignee_name"),
                "assignee_seat_slug": fleet_roster.seat_slug_for_email(assignee_email),
                "occupied": occupant is not None,
                "occupant": occupant,
                "blocked_by": [
                    {
                        "id": row["id"],
                        "key": row.get("key"),
                        "title": row.get("title"),
                        "status": row.get("status"),
                    }
                    for row in blockers
                ],
                "checkout_hint": checkout_hint(
                    issue_key=issue.get("key"),
                    issue_id=issue_id,
                    seat_slug=(None if occupant is None else occupant.get("seat_slug"))
                    or fleet_roster.seat_slug_for_email(assignee_email),
                ),
            }
        )

    occupied = sum(1 for chair in chairs if chair["occupied"])
    return {
        "schema": FLOOR_SCHEMA,
        "project": {
            "id": project["id"],
            "key": project.get("key"),
            "name": project.get("name"),
        },
        "chairs": chairs,
        "chair_count": len(chairs),
        "occupied_count": occupied,
        "empty_count": len(chairs) - occupied,
        "protocol": PROTOCOL,
        "semantics": {
            "snapshot": "this_project_right_now",
            "does_not_assert": ["alive", "ready", "unblocked", "running"],
        },
    }
