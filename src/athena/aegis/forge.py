"""The landing rule: turning an authenticated forge claim into issue history.

``core/forge_events`` reads a payload; this decides what Athena does with it. The
whole feature is one sentence: **an inbound event that names an issue Athena
knows lands as an imported activity event on that issue, and one that does not is
counted.**

Everything interesting follows from *imported*:

  * Migration 0041 marks foreign history so it can never be mistaken for something
    Athena did. Every native-only mechanism excludes it — undo refuses an
    imported event, the automation scan reads the stream with ``native_only=True``,
    and the lifecycle facts (0055), claim handoffs (0058),
    assignee facts (0068), fleet metrics, the attention rollup, and the security
    refusal counters all filter on ``imported_at IS NULL`` (the scan, the rollup,
    and the counters added after the adversarial review found the guard missing
    where this docstring claimed it). Landing forge events this way inherits
    every one of those exclusions rather than re-deriving them, which is exactly
    why the rule is cheap **and** safe: a forge cannot move an issue's status,
    cannot alter a completion-cycle median, and cannot be undone into a native
    write.
  * A forge event carries **no run coordinates**. It belongs to no Athena run, so
    it can never be spliced into one's replay. ``activity.record`` enforces that
    for every imported row.

The actor is the operator who **registered the source**. A forge event has no
Athena identity behind it — the person who pushed the commit may have no account
here at all — and inventing a synthetic user would put a fictional name on the
trail. Attributing it to the operator who granted the source is the honest
reading: *you allowed this channel, and this is what came through it.*

Key resolution is where false positives die. ``extract_keys`` returns anything
shaped like a key, including ``UTF-8`` and ``ISO-8601``; only a prefix matching a
real project, and a number matching a real issue in it, becomes a landing.
"""

from __future__ import annotations

import sqlite3

from athena.core import activity, db, event_sources, forge_events

#: One verb per event kind. Distinct verbs (rather than one `forge_event`) so the
#: trail reads as English and so a filter can ask for merges without reading
#: details.
VERBS = {
    forge_events.KIND_PUSH: "forge_commit",
    forge_events.KIND_PULL_REQUEST: "forge_pull_request",
    forge_events.KIND_BRANCH: "forge_branch",
}


def resolve_issue(conn: sqlite3.Connection, prefix: str, number: int) -> int | None:
    """The issue id for ``PREFIX-number``, or None.

    This is the filter that separates an issue key from a character encoding: the
    prefix must be a real project key (``projects.key`` is ``COLLATE NOCASE``) and
    the number a real ``project_seq`` within it. ``UTF-8`` resolves to nothing
    unless a project is literally keyed ``UTF``.
    """
    row = conn.execute(
        "SELECT i.id FROM issues i JOIN projects p ON p.id = i.project_id "
        "WHERE p.key = ? AND i.project_seq = ?",
        (prefix, number),
    ).fetchone()
    return row["id"] if row else None


def land_delivery(
    conn: sqlite3.Connection, *, source: dict, facts: list[forge_events.ForgeFact]
) -> dict:
    """Record every fact that names a known issue, and count the rest.

    Returns ``{"landed": [...], "matched": n, "unmatched": n}``. The whole
    delivery — every landed event and the source's counters — commits together, so
    a partially-applied delivery is not a state this can reach.

    Bounded by ``MAX_ISSUES_PER_DELIVERY``: a single payload naming thirty issues
    is a mass rename or an attempt to spray the trail, and neither should be able
    to write thirty rows. Facts past the ceiling count as unmatched, so the
    operator still sees that something arrived and did not land.
    """
    landed: list[dict] = []
    unmatched = 0
    seen: set[tuple[int, str]] = set()

    with db.transaction(conn, immediate=True):
        # The DB clock, in the same 'YYYY-MM-DD HH:MM:SS' shape portability writes
        # (core/portability stamps imported_at from datetime('now')). One format
        # for the column keeps "everything foreign that arrived in this window" a
        # single comparable query across both entry paths.
        imported_at = conn.execute("SELECT datetime('now')").fetchone()[0]
        for fact in facts:
            issue_ids = []
            for prefix, number in fact.keys:
                issue_id = resolve_issue(conn, prefix, number)
                if issue_id is not None:
                    issue_ids.append(issue_id)
            if not issue_ids:
                # Authentic, well-formed, and about nothing this workspace tracks.
                unmatched += 1
                continue
            for issue_id in issue_ids:
                # One fact naming the same issue twice (branch and title agreeing)
                # lands once.
                fingerprint = (issue_id, fact.summary)
                if fingerprint in seen:
                    continue
                if len(seen) >= forge_events.MAX_ISSUES_PER_DELIVERY:
                    unmatched += 1
                    continue
                seen.add(fingerprint)
                detail = f"{source['name']}: {fact.summary}"
                if fact.ref:
                    detail = f"{detail} — {fact.ref}"
                event = activity.record(
                    conn,
                    actor_id=source["created_by"],
                    verb=VERBS[fact.kind],
                    target_kind="issue",
                    target_id=issue_id,
                    detail=detail[
                        : forge_events.MAX_SUMMARY_CHARS + forge_events.MAX_REF_CHARS
                    ],
                    commit=False,
                    # The whole point: foreign history, excluded from every
                    # native-only mechanism by 0041's existing guards.
                    imported_at=imported_at,
                )
                landed.append({"issue_id": issue_id, "event_id": event["id"]})
        event_sources.record_delivery(
            conn,
            source["id"],
            matched=len(landed),
            unmatched=unmatched,
            commit=False,
        )
    return {"landed": landed, "matched": len(landed), "unmatched": unmatched}
