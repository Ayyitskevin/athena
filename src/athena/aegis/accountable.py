"""Who answers for a holder's work on an issue.

A lease is held by one account, and other accounts answer for what happens to
it: the issue's assignee, and the account that delegated the issue to the
holder. A signal about the holder (its budget ran out, it keeps renewing with
no recorded work) is told to them, never to the holder, and only to an account
that still exists and may still see the issue.
"""

from __future__ import annotations

import sqlite3

from athena.core import access, users


def answering_for(
    conn: sqlite3.Connection, *, holder_id: int, issue_id: int
) -> list[int]:
    """The accounts that answer for ``holder_id``'s work on this issue and may
    still see it: the assignee and the account that delegated it to the
    holder. Never the holder."""
    row = conn.execute(
        "SELECT i.project_id, i.assignee_id, "
        "(SELECT c.added_by FROM issue_contributors c "
        "WHERE c.issue_id = i.id AND c.user_id = ?) AS delegated_by "
        "FROM issues i WHERE i.id = ?",
        (holder_id, issue_id),
    ).fetchone()
    if row is None:
        return []
    recipients = []
    for user_id in {row["assignee_id"], row["delegated_by"]} - {None, holder_id}:
        user = users.get_user(conn, int(user_id))
        if user is None or user.get("removed_at") is not None:
            continue
        if access.can_see_project_or_backlog(conn, user, row["project_id"]):
            recipients.append(int(user_id))
    return sorted(recipients)
