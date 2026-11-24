"""How many times a lease has been renewed with no recorded work (C7).

A renewal keeps a possession's window open and a heartbeat says the process
is alive. Neither says the work moved. A holder that only renews and
heartbeats reads as ``pinging`` in the run state, and nothing raised it: the
issue stayed out of every other seat's reach for as long as the holder kept
asking.

The count is derived from the trail on every read: the holder's native
``lease_renewed`` events for this lease's generation that are newer than the
holder's newest recorded work on the issue
(``work_progress.SUBSTANTIVE_WORK_VERBS``). Any such work by the holder starts
the count again. Work by another account does not: someone else commenting is
not the holder making progress.

The proxy's limit is ``work_progress``'s: work the holder records somewhere
else (another issue, a page, a repository) is not seen here. The answer for a
holder with nothing to change on the issue is a comment saying where it is.
"""

from __future__ import annotations

import sqlite3

from athena import config
from athena.aegis import work_progress

#: The event recorded, once, when the count reaches the threshold.
VERB_NO_PROGRESS = "no_progress"
#: The attention reason a live lease at or past the threshold carries.
REASON_NO_PROGRESS = "no_progress"

_WORK = ",".join("?" for _ in work_progress.SUBSTANTIVE_WORK_VERBS)


def threshold() -> int:
    """Renewals with no recorded work before the claim is raised; 0 never
    raises (``ATHENA_NO_PROGRESS_RENEWALS``)."""
    return config.NO_PROGRESS_RENEWALS


def renewals_without_progress(
    conn: sqlite3.Connection, *, issue_id: int, holder_id: int, generation: str
) -> int:
    """The holder's renewals of this possession since its newest recorded work
    on the issue. Counts a renewal recorded earlier in the caller's
    transaction."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM activity r "
        "JOIN activity_event_facts f ON f.event_id = r.id "
        "WHERE r.target_kind = 'issue' AND r.target_id = ? "
        "AND r.verb = 'lease_renewed' AND r.actor_id = ? "
        "AND r.imported_at IS NULL AND f.generation = ? "
        "AND r.id > COALESCE((SELECT MAX(w.id) FROM activity w "
        "WHERE w.target_kind = 'issue' AND w.target_id = ? AND w.actor_id = ? "
        f"AND w.verb IN ({_WORK}) AND w.imported_at IS NULL), 0)",
        (
            issue_id,
            holder_id,
            generation,
            issue_id,
            holder_id,
            *work_progress.SUBSTANTIVE_WORK_VERBS,
        ),
    ).fetchone()
    return int(row["n"])


def raised(count: int) -> bool:
    """Whether a lease with this count is raised for attention."""
    limit = threshold()
    return limit > 0 and count >= limit
