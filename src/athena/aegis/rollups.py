"""Live parent rollups — a parent issue's children, counted by status category.

One owner for one number. Before this module the issue page summed
``statuses.is_done`` over the children inline in the web layer, which meant the
count could not be reused (an embed had to re-derive it) and the web layer was
doing arithmetic it does not own. Everything that wants "how is this parent
going?" now calls :func:`child_rollup`.

**Computed at read time, never stored.** There is no denormalized progress
column and there must not be: a cached rollup is stale the moment a child moves
and is a visibility leak the moment a different viewer opens the page. The whole
answer comes from one gated GROUP BY.

**Done-ness is a category, never a status name.** A project whose finished state
is called ``shipped`` must roll up exactly like one that calls it ``done``, so
the bucket comes from ``statuses.category_sql`` — the generated expression — and
never from a comparison against a literal status.

**Two things the rollup deliberately says out loud.** Archived children are
excluded from the bars (abandoned work should not sit in a denominator forever,
and ``list_issues`` already treats archived as gone by default), and the count
excluded is reported rather than dropped, because a progress bar that quietly
omits rows is the same quiet lie an unlabelled partial graph would be. Children
the viewer cannot see are excluded by the same gate the children list uses, and
those are NOT reported: reporting them would turn the bar into an existence
oracle for private work.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import statuses

#: The bucket order a progress line reads in. Taken from the status vocabulary
#: itself rather than re-declared, so a fourth category could never mean one
#: thing here and another there.
BUCKETS = statuses.CATEGORIES

_CATEGORY = statuses.category_sql(
    status_column="c.status", category_column="cps.category"
)
# Each child resolves against ITS OWN project's status set: parenting has no
# same-project rule, so a child in another project brings its own vocabulary.
_JOIN = (
    " LEFT JOIN project_statuses cps"
    " ON cps.project_id = c.project_id AND cps.name = c.status"
)


def _visibility_clause(visible_project_ids: set[int] | None) -> tuple[str, list]:
    """The same three-branch gate ``issues.list_children`` applies, as SQL.

    None means no filtering (admin/internal); an empty set means the viewer sees
    no project and so may only see backlog children. Collapsing those two would
    either blank an admin's rollup or leak every private child.
    """
    if visible_project_ids is None:
        return "", []
    if not visible_project_ids:
        return " AND c.project_id IS NULL", []
    placeholders = ",".join("?" for _ in visible_project_ids)
    return (
        f" AND (c.project_id IS NULL OR c.project_id IN ({placeholders}))",
        sorted(visible_project_ids),
    )


def _percent_done(done: int, total: int) -> int:
    """The headline percentage, with the two ends reserved for the truth.

    Plain rounding would round 199/200 up to a triumphant "100% done" while a
    child is still open, and 1/200 down to "0% done" after real work landed.
    Both are small lies in the one number a reader takes at a glance, so 100 is
    reserved for everything being done and 0 for nothing being done; every state
    in between is clamped into 1-99.
    """
    if not total:
        return 0
    if done == total:
        return 100
    if done == 0:
        return 0
    return min(99, max(1, round(done * 100 / total)))


def child_rollup(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    visible_project_ids: set[int] | None = None,
) -> dict:
    """How one parent's direct children are distributed across status categories.

    Returns ``{counts, total, done, percent_done, archived_excluded, has_children}``
    where ``counts`` holds every bucket in :data:`BUCKETS` (zero-filled, so a
    caller never has to guess whether a missing key means none or unknown).

    ``percent_done`` is an integer 0-100 over the counted children, and is 0 when
    there are none — a parent with no live children has no progress to report,
    which is different from being finished. A child whose status has no row and
    is not one of the defaults lands in ``doing``: unknown is never counted as
    done, matching ``statuses.is_done``.
    """
    clause, params = _visibility_clause(visible_project_ids)
    # The alias is `bucket`, never `category`: the LEFT JOIN brings a real
    # `project_statuses.category` column into scope, and `GROUP BY category`
    # binds to THAT column rather than to this expression — which silently
    # groups every backlog child (whose joined category is NULL) into one row
    # and reports one arbitrary child's category for all of them.
    rows = conn.execute(
        f"SELECT {_CATEGORY} AS bucket, COUNT(*) AS n"
        f" FROM issues c{_JOIN}"
        f" WHERE c.parent_id = ? AND c.archived_at IS NULL{clause}"
        " GROUP BY bucket",
        [issue_id, *params],
    ).fetchall()
    counts = {bucket: 0 for bucket in BUCKETS}
    for row in rows:
        # A category outside the vocabulary cannot come from category_sql, but a
        # hand-edited project_statuses row could carry one; count it as doing
        # rather than dropping it, so the total always equals the children shown.
        bucket = row["bucket"] if row["bucket"] in counts else "doing"
        counts[bucket] += int(row["n"])

    archived = conn.execute(
        f"SELECT COUNT(*) AS n FROM issues c"
        f" WHERE c.parent_id = ? AND c.archived_at IS NOT NULL{clause}",
        [issue_id, *params],
    ).fetchone()

    total = sum(counts.values())
    done = counts["done"]
    return {
        "counts": counts,
        # Each bucket's share of the bar, computed once here so the page and the
        # embed place identical widths instead of each doing the arithmetic.
        "segments": [
            {
                "bucket": bucket,
                "count": counts[bucket],
                "percent": round(counts[bucket] * 100 / total, 2),
            }
            for bucket in ("done", "doing", "todo")
            if counts[bucket]
        ]
        if total
        else [],
        "total": total,
        "done": done,
        "percent_done": _percent_done(done, total),
        "archived_excluded": int(archived["n"]),
        "has_children": total > 0 or int(archived["n"]) > 0,
    }
