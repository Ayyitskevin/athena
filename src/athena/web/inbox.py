"""The inbox as its reader sees it (U8).

The inbox listed one row per notification as its raw verb and a target
number: ``Sol claim_completed issue #12``. After fifteen agent actions an
operator had 34 unread rows about a handful of issues, in arrival order,
with no way to see only what was asked of them.

This module is presentation only. It says each verb in words, puts rows
under the issue or page they are about (headed by its key and title), and
sorts every verb into one of five kinds the page can filter by. It reads
nothing but the headings; the rows, their gating and their state come from
the notification reads.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import issues
from athena.mentor import pages

#: The kinds a reader filters by, in the order the page offers them.
KINDS = (
    ("asks", "Asked of you"),
    ("completions", "Completions"),
    ("comments", "Comments"),
    ("claims", "Claims"),
    ("changes", "Changes"),
)

_KIND = {
    "assigned": "asks",
    "delegated": "asks",
    "added_contributor": "asks",
    "review_requested": "asks",
    "approval_requested": "asks",
    "run_control_requested": "asks",
    "claim_completed": "completions",
    "commented": "comments",
    "comment_edited": "comments",
    "comment_deleted": "comments",
    "page_commented": "comments",
    "page_comment_edited": "comments",
    "page_comment_deleted": "comments",
    "claimed": "claims",
    "lease_renewed": "claims",
    "claim_yielded": "claims",
    "claim_handoff_resumed": "claims",
    "lease_lapsed": "claims",
    "lease_released": "claims",
    "no_progress": "claims",
    "holder_budget_exhausted": "claims",
    "delegation_declined": "claims",
}

# What each verb says, before the name of what it is about. A verb with no
# entry is said as itself with its underscores as spaces.
_WORDS = {
    "created": "created",
    "issue_edited": "edited",
    "changed_status": "changed the status of",
    "changed_priority": "changed the priority of",
    "changed_project": "moved",
    "assigned": "changed who is assigned to",
    "unassigned": "unassigned",
    "delegated": "delegated",
    "added_contributor": "added a contributor to",
    "removed_contributor": "removed a contributor from",
    "delegation_declined": "declined",
    "review_requested": "asked for a review on",
    "approval_requested": "asked for approval on",
    "children_done": "finished the last open sub-issue of",
    "commented": "commented on",
    "comment_edited": "edited a comment on",
    "comment_deleted": "deleted a comment on",
    "claimed": "claimed",
    "lease_renewed": "renewed the claim on",
    "claim_completed": "completed a claim on",
    "claim_yielded": "yielded the claim on",
    "claim_handoff_resumed": "resumed a handed-off claim on",
    "lease_lapsed": "let the claim lapse on",
    "lease_released": "released the claim on",
    "no_progress": "renewed a claim with no recorded work on",
    "holder_budget_exhausted": "ran out of budget while holding",
    "archived": "archived",
    "labeled": "labeled",
    "unlabeled": "removed a label from",
    "linked": "linked",
    "unlinked": "unlinked",
    "set_parent": "set the parent of",
    "removed_parent": "removed the parent of",
    "moved_to_sprint": "moved to a sprint",
    "removed_from_sprint": "took out of its sprint",
    "added_attachment": "attached a file to",
    "removed_attachment": "removed a file from",
    "page_created": "created",
    "page_edited": "edited",
    "page_moved": "moved",
    "page_archived": "archived",
    "page_restored": "restored",
    "page_deleted": "deleted",
    "page_commented": "commented on",
    "page_comment_edited": "edited a comment on",
    "page_comment_deleted": "deleted a comment on",
    "page_labeled": "labeled",
    "page_unlabeled": "removed a label from",
    "page_learning_recorded": "recorded a learning on",
}


def kind_of(verb: str) -> str:
    """Which of :data:`KINDS` a verb belongs to; anything unlisted is a change."""
    return _KIND.get(verb, "changes")


def words(verb: str) -> str:
    """The verb as a reader says it."""
    return _WORDS.get(verb, verb.replace("_", " "))


# How every lease event's detail begins: the lease's machine record, with its
# generation token. It stays on the trail and in the issue's history.
_LEASE_RECORD = "generation "


def detail_text(row: dict) -> str | None:
    """The detail a row shows after its words, or none. A lease event's
    detail is its machine record (a generation hash, ids, a path fence), which
    the row's own words already say in plain terms."""
    detail = row.get("detail") or ""
    return None if not detail or detail.startswith(_LEASE_RECORD) else detail


def kind_filter(value: str | None) -> str | None:
    """A requested kind when it is one, else none (every kind)."""
    return value if value in dict(KINDS) else None


def kind_counts(rows: list[dict]) -> dict[str, int]:
    """How many of ``rows`` are of each kind, zero included."""
    counts = {name: 0 for name, _ in KINDS}
    for row in rows:
        counts[kind_of(row["verb"])] += 1
    return counts


def grouped(
    conn: sqlite3.Connection, rows: list[dict], *, kind: str | None = None
) -> list[dict]:
    """``rows`` (newest first) of ``kind`` under what each is about.

    A group is one issue or page: its link, its key and title, how many of
    its rows are unread, and its rows, newest first. Groups keep the order of
    their newest row, so the page still reads newest first. A target whose
    heading cannot be read (deleted since) is headed by its number alone.
    """
    shown = [row for row in rows if kind is None or kind_of(row["verb"]) == kind]
    issue_ids = {row["target_id"] for row in shown if row["target_kind"] == "issue"}
    page_ids = {row["target_id"] for row in shown if row["target_kind"] == "page"}
    headings: dict[tuple[str, int], dict[str, str | None]] = {
        ("issue", issue_id): heading
        for issue_id, heading in issues.headings_for_ids(conn, issue_ids).items()
    }
    headings.update(
        (("page", page_id), {"key": None, "title": title})
        for page_id, title in pages.titles_for_ids(conn, page_ids).items()
    )
    groups: dict[tuple[str, int], dict] = {}
    for row in shown:
        target = (row["target_kind"], row["target_id"])
        group = groups.get(target)
        if group is None:
            heading = headings.get(target, {"key": None, "title": None})
            group = groups[target] = {
                "kind": row["target_kind"],
                "id": row["target_id"],
                "href": row["href"],
                "key": heading["key"],
                "title": heading["title"],
                "unread": 0,
                "rows": [],
            }
        group["rows"].append(
            {**row, "words": words(row["verb"]), "detail_text": detail_text(row)}
        )
        group["unread"] += row["read_at"] is None
    return list(groups.values())
