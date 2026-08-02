"""The Field Guide — an agent's manual, seeded as ordinary pages.

Office ships its help inside Office. An agent's manual should be pages in the
same knowledge layer it uses for everything else: readable with the same MCP
tools, linkable from issues, searchable, exportable. So the guide is not a
special surface — it is a space, and everything true of a space is true of it.

The content lives beside this module as markdown (``field_guide/*.md``), loaded
rather than inlined, and travels in the wheel as package data — the same shape
``core/migrations`` uses, and pinned by the same wheel-manifest gate.

Seeding goes through the ordinary commands as a real author, so the pages carry
genuine provenance and the activity chain covers them. There is one
implementation here; ``athena-demo --field-guide`` and ``athena-field-guide``
are two entry points to it, never two seeders.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from athena.core import labels, links
from athena.mentor import page_commands, space_commands, spaces

CONTENT_DIR = Path(__file__).parent / "field_guide"

#: The space the guide lives in. Its key is what makes seeding idempotent: a
#: second run finds it and refuses rather than overwriting pages an operator may
#: since have edited. Their workspace, their pages.
GUIDE_SPACE_KEY = "GUIDE"
GUIDE_SPACE_NAME = "Athena Field Guide"
GUIDE_SPACE_DESCRIPTION = (
    "How to work in this workspace, addressed to the agents who work in it."
)

#: The label that makes a page instantiable as work (see mentor/page_templates
#: for the same pattern with 'template').
PLAYBOOK_LABEL = "playbook"

#: (file, title, is_playbook) in reading order. A manifest rather than parsed
#: front matter: the order and the titles are decisions, and a decision belongs
#: in code where it can be read, not in a heading a content edit could move.
PAGES: tuple[tuple[str, str, bool], ...] = (
    ("01_your_desk.md", "Your desk", False),
    ("02_claiming_work.md", "Claiming and yielding work", False),
    ("03_recording_learnings.md", "Recording what a run learned", False),
    ("04_run_controls.md", "Answering a run control", False),
    ("05_playbooks.md", "Playbooks: docs that start work", False),
    ("06_searching.md", "Searching the workspace", False),
    ("07_watching.md", "Watching shared memory", False),
    ("08_the_trail.md", "What the trail proves", False),
    ("09_example_playbook.md", "Example playbook: shipping a change", True),
)


class FieldGuideError(Exception):
    """A safe, user-actionable refusal while seeding the guide."""


def page_body(filename: str) -> str:
    """Read one guide page's markdown from package data.

    Raises :class:`FieldGuideError` when the file is missing, which in a built
    wheel means the package-data glob dropped it — a failure worth naming rather
    than seeding a guide with a hole in it."""
    path = CONTENT_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FieldGuideError(
            f"field guide content missing from the package: {filename}"
        ) from exc


def seed_field_guide(conn: sqlite3.Connection, *, author_id: int) -> dict:
    """Create the GUIDE space and its pages, authored by ``author_id``.

    Refuses with :class:`FieldGuideError` if a space with the guide's key already
    exists. That is the whole idempotency story: re-running never overwrites, so
    an operator who has edited these pages keeps their edits, and an operator who
    wants a fresh copy deletes the space first and says so.

    Every write goes through the mentor commands, so each page lands with its own
    activity event, its links indexed, and its row in the hash chain — the guide
    is part of the workspace's history rather than something injected beneath it.
    """
    if spaces.get_space_by_key(conn, GUIDE_SPACE_KEY) is not None:
        raise FieldGuideError(
            f"a space with key {GUIDE_SPACE_KEY} already exists — "
            "delete it first if you want a fresh field guide"
        )
    space = space_commands.create_space(
        conn,
        actor_id=author_id,
        key=GUIDE_SPACE_KEY,
        name=GUIDE_SPACE_NAME,
        description=GUIDE_SPACE_DESCRIPTION,
    )
    # Reuse the operator's existing 'playbook' label if there is one — a second
    # label with the same meaning would split every playbook query in half.
    label = labels.get_label_by_name(conn, PLAYBOOK_LABEL) or labels.create_label(
        conn, name=PLAYBOOK_LABEL
    )

    created: list[dict] = []
    for filename, title, is_playbook in PAGES:
        page = page_commands.create_page(
            conn,
            actor_id=author_id,
            space_id=space["id"],
            title=title,
            body=page_body(filename),
        )
        if is_playbook:
            page_commands.attach_page_label(
                conn, actor_id=author_id, page_id=page["id"], label_id=label["id"]
            )
        created.append(page)

    # Re-derive the link index once every page exists.
    #
    # A [[Title]] wikilink resolves only against a page that is already there, and
    # records NOTHING when it does not resolve — unlike a numeric [[page:N]] ref,
    # which is stored broken and lights up later. The guide's pages reference each
    # other in both directions, so on the first pass roughly half the references
    # pointed at pages this loop had not created yet, and a manual full of dead
    # references to itself is not a manual.
    #
    # This re-runs the same derivation an ordinary edit triggers, on bodies that
    # have not changed. It writes no page, records no event, and is not a second
    # way to author content — it is the derived index catching up to a batch.
    for page in created:
        links.sync_links(
            conn,
            source_kind="page",
            source_id=page["id"],
            body=page["body"],
            commit=False,
        )
    conn.commit()
    return {
        "space": space,
        "pages": created,
        "page_count": len(created),
        "author_id": author_id,
    }
