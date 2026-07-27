"""Page templates, and the operator's daily note.

A wiki without templates makes every recurring document an act of remembering:
what sections a runbook has, what a postmortem must capture, what you check each
morning. Templates make the structure the default.

**There is no template table, and no `is_template` column.** A template is a page
carrying the ``template`` label — the label machinery that already exists, with
the audited attach/detach commands that already exist. That is a deliberate
choice against schema, and it is worth stating why, because the obvious
alternative (a migration adding a boolean) looked cheaper for about five minutes:

  * A template is a page. It is written, versioned, commented on, linked to, and
    archived exactly like a page, and every one of those behaviours would have to
    be re-granted to a separate table.
  * Marking one is already an audited, reversible, attributed write — the label
    commands emit their own activity events. A new column would have needed a new
    command, a new verb, and a new undo compensator to reach the same standard.
  * The set of templates is a *query*, and Athena already answers it.

The cost is honest and small: a workspace that was already using a label called
"template" for something else would see those pages offered as templates. The
label is the marker, so that is a naming collision, not a data problem — detach
the label and the page stops being a template.

Substitution is deliberately tiny: ``{{title}}`` and ``{{date}}``, nothing else.
A template language is a programming language, and a page body is not a place to
run one. Unknown ``{{...}}`` is left exactly as written rather than erroring or
blanking — it is content, and the author may well have meant it literally.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3

from athena.core import labels

# The label that marks a page as a template. Case-insensitive in practice: the
# labels table is UNIQUE COLLATE NOCASE, so "Template" and "template" are one.
TEMPLATE_LABEL = "template"

# The template a daily note is built from, looked up by title within the space.
# A space without one still gets daily notes — they simply start empty — so the
# feature never depends on setup the operator has not done yet.
DAILY_TEMPLATE_TITLE = "Daily Note Template"

# The two substitutions, and the only two. Spacing inside the braces is tolerated
# because a human will type both forms.
_VAR_RE = re.compile(r"\{\{\s*(title|date)\s*\}\}")


def today(now: dt.datetime | None = None) -> str:
    """Today's date as ``YYYY-MM-DD``, in UTC.

    UTC, not local time, because everything else durable in Athena is UTC
    (``datetime('now')`` in SQL, the automation schedules, the activity trail).
    An operator west of Greenwich will roll over to a new daily note before their
    own midnight; that is a real limitation, written down in docs/GRAPH.md rather
    than papered over with a guess at a timezone Athena does not store.
    """
    stamp = now or dt.datetime.now(dt.UTC)
    return stamp.strftime("%Y-%m-%d")


def fill(body: str | None, *, title: str, date: str) -> str:
    """Substitute ``{{title}}``/``{{date}}`` in a template body."""
    values = {"title": title, "date": date}
    return _VAR_RE.sub(lambda m: values[m.group(1).strip()], body or "")


def list_templates(conn: sqlite3.Connection, space_id: int) -> list[dict]:
    """Live template pages in one space, title order.

    Scoped to the space because a template is offered where it is used: a
    postmortem template in Engineering has no business appearing in a personal
    notes space. Archived pages are excluded — an archived template is one
    someone retired.
    """
    page_ids = labels.page_ids_for_label(conn, TEMPLATE_LABEL)
    if not page_ids:
        return []
    placeholders = ",".join("?" for _ in page_ids)
    rows = conn.execute(
        f"SELECT * FROM pages WHERE id IN ({placeholders}) "
        f"AND space_id = ? AND archived_at IS NULL ORDER BY title, id",
        [*page_ids, space_id],
    ).fetchall()
    return [dict(row) for row in rows]


def is_template(conn: sqlite3.Connection, page_id: int) -> bool:
    """Whether this page carries the template label."""
    return any(
        label["name"].lower() == TEMPLATE_LABEL
        for label in labels.labels_for_page(conn, page_id)
    )


def daily_template(conn: sqlite3.Connection, space_id: int) -> dict | None:
    """The space's daily-note template, if it has one.

    Matched by title AND the template label: a page merely *named* "Daily Note
    Template" is not silently treated as one, because the label is what makes a
    page a template everywhere else in this module and two rules for the same
    thing is how drift starts.
    """
    for page in list_templates(conn, space_id):
        if page["title"].strip().lower() == DAILY_TEMPLATE_TITLE.lower():
            return page
    return None
