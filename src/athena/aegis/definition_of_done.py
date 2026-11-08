"""A project's Definition of Done: an ordinary Mentor page, shown and reported against.

The checklist is a page carrying the shared label ``definition-of-done:<PROJECT KEY>``; that one
label is both the purpose and the link to the project, so nothing new is stored. Each ``-``, ``*``
or ``+`` line of the page (optionally with a ``[ ]`` / ``[x]`` box) is one item.

A completion report is annotated "N of M checklist items reported" by matching each result's
``subject`` entries to the items exactly, after folding case and whitespace. "Reported" means a
result named the item, whatever its state: the annotation says what the reporter covered, never
that it passed. Nothing is refused or gated: refusing an incomplete report would need a
per-project policy column, which is the operator's decision to make.

The checklist is read at the viewer's own visibility (project and space), so a hidden page, or a
count that would reveal its length, is never shown. Coverage is computed against the page as it is
now, not as it was when the report was written.
"""

from __future__ import annotations

import re
import sqlite3

from athena.core import access, labels

LABEL_PREFIX = "definition-of-done:"
MAX_ITEMS = 30
MAX_ITEM_CHARS = 300
NOTE = (
    "advisory checklist from a project page; completion reports are annotated against it, "
    "and it is not enforced"
)
_ITEM = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?(\S.*?)\s*$")


def normalize(text: str) -> str:
    """Case- and whitespace-folded form used to compare a subject with an item."""
    return " ".join(text.split()).casefold()[:MAX_ITEM_CHARS]


def _items(body: str) -> tuple[list[str], bool]:
    items: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        match = _ITEM.match(line)
        if match is None:
            continue
        text = " ".join(match.group(1).split())[:MAX_ITEM_CHARS]
        key = normalize(text)
        if key in seen:
            continue
        if len(items) == MAX_ITEMS:
            return items, True
        seen.add(key)
        items.append(text)
    return items, False


def checklist(
    conn: sqlite3.Connection, project_id: int | None, *, actor: dict | None
) -> dict | None:
    """The project's checklist as this actor may see it, or ``None``.

    With several labelled pages the oldest visible one is the checklist. ``None`` also covers
    a project the actor cannot see, a space they cannot see, an archived page and an empty list.
    """
    if project_id is None or not access.can_see_project(conn, actor, project_id):
        return None
    project = conn.execute(
        "SELECT key FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        return None
    name = f"{LABEL_PREFIX}{project['key']}"
    for page_id in sorted(labels.page_ids_for_label(conn, name)):
        page = conn.execute(
            "SELECT p.id, p.title, p.body, p.space_id, s.key AS space_key "
            "FROM pages p JOIN spaces s ON s.id = p.space_id "
            "WHERE p.id = ? AND p.archived_at IS NULL",
            (page_id,),
        ).fetchone()
        if page is None or not access.can_see_space(conn, actor, page["space_id"]):
            continue
        items, truncated = _items(page["body"])
        if not items:
            continue
        return {
            "page_id": page["id"],
            "title": page["title"],
            "space_id": page["space_id"],
            "space_key": page["space_key"],
            "label": name,
            "items": items,
            "truncated": truncated,
            "note": NOTE,
        }
    return None


def coverage(
    conn: sqlite3.Connection,
    project_id: int | None,
    *,
    actor: dict | None,
    report: dict | None,
) -> dict | None:
    """``N of M checklist items reported`` for one report, or ``None`` when there is no
    report or this actor sees no checklist for the project."""
    if report is None:
        return None
    found = checklist(conn, project_id, actor=actor)
    if found is None:
        return None
    named = {
        normalize(subject)
        for result in report.get("results", [])
        for subject in result.get("subject", [])
    }
    rows = [
        {"item": item, "reported": normalize(item) in named} for item in found["items"]
    ]
    reported = sum(row["reported"] for row in rows)
    return {
        "reported": reported,
        "total": len(rows),
        "summary": f"{reported} of {len(rows)} checklist items reported",
        "items": rows,
    }
