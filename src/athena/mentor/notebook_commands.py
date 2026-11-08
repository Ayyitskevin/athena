"""Find-or-create an agent's notebook: one labelled page in a private space it owns.

The notebook is the agent's own memory between sessions. It is created by the
agent's explicit call, on first use; reading the desk never creates it.
Creation is one ``BEGIN IMMEDIATE`` transaction, so two first calls from parallel
sessions of one seat serialise and the second finds the first's page. It folds:

1. a space created by the agent, made private (the agent is its creator and
   member, and an admin sees every space — so exactly that agent and the
   operator can read it);
2. one page in that space;
3. the ``NOTEBOOK_LABEL`` label on that page;

each with its ordinary audit event, through the same data-layer writers the
space and page commands use. The already-exists path writes nothing, charges no
budget and records no event (the daily-note find-or-create's rule).

After that the notebook is an ordinary page: the agent appends with the ordinary
If-Match page edit, and page versions keep every revision. Nothing here
summarises it, and it is not a runbook — runbooks stay the vetted store.
"""

from __future__ import annotations

import sqlite3

from athena.core import access, budgets, db, labels
from athena.core.agent_notebook import (
    NOTEBOOK_EXCERPT_LINES,
    NOTEBOOK_LABEL,
    find_notebook,
    notebook_space_key,
)
from athena.mentor import page_activity, pages, space_activity, spaces

__all__ = [
    "NOTEBOOK_EXCERPT_LINES",
    "NOTEBOOK_LABEL",
    "NotebookCommandError",
    "ensure_agent_notebook",
    "notebook_space_key",
]

#: How many alternative keys to try when the preferred one is already taken by
#: some other space. A clash is a coincidence, not a pattern; past this, refuse.
_KEY_ATTEMPTS = 20

_INITIAL_BODY = (
    "Notes this agent keeps between sessions. Append at session end with an "
    "If-Match edit of this page; the first lines appear on the desk."
)


class NotebookCommandError(Exception):
    """A transport-neutral refusal with a kind the adapter maps to a status."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _free_key(conn: sqlite3.Connection, user_id: int) -> str:
    base = notebook_space_key(user_id)
    for attempt in range(_KEY_ATTEMPTS):
        key = base if attempt == 0 else f"{base}-{attempt + 1}"
        if spaces.get_space_by_key(conn, key) is None:
            return key
    raise NotebookCommandError(
        "conflict", f"no free notebook space key near {base}; free one of them"
    )


def ensure_agent_notebook(
    conn: sqlite3.Connection, *, actor: dict
) -> tuple[dict, bool]:
    """Return ``(page, created)`` for this agent's notebook, creating it on the
    first call. Only an agent account has one; anyone else is refused."""
    if not actor.get("is_agent"):
        raise NotebookCommandError("forbidden", "a notebook belongs to an agent seat")
    user_id = int(actor["id"])
    with db.transaction(conn, immediate=True):
        existing = find_notebook(conn, user_id)
        if existing is not None:
            page = pages.get_page(conn, int(existing["id"]))
            assert page is not None
            return page, False

        budgets.charge(conn, actor)
        name = str(actor.get("name") or f"agent {user_id}")
        space = spaces.create_space(
            conn,
            key=_free_key(conn, user_id),
            name=f"Notebook: {name}",
            description="This agent's notebook. Visible to the agent and the operator.",
            created_by=user_id,
            commit=False,
        )
        space_activity.record_space_created(
            conn,
            actor_id=user_id,
            space_id=space["id"],
            name=space["name"],
            commit=False,
        )
        if space.get("visibility") != "private":
            updated = spaces.set_visibility(conn, space["id"], "private", commit=False)
            assert updated is not None
            space_activity.record_space_visibility_changed(
                conn,
                actor_id=user_id,
                space_id=space["id"],
                name=updated["name"],
                visibility="private",
                commit=False,
            )
        access.add_space_member(
            conn, space["id"], user_id, added_by=user_id, commit=False
        )

        page = pages.create_page(
            conn,
            space_id=space["id"],
            title=f"Notebook: {name}",
            body=_INITIAL_BODY,
            created_by=user_id,
            commit=False,
        )
        page_activity.record_page_created(
            conn,
            actor_id=user_id,
            page_id=page["id"],
            title=page["title"],
            body=page["body"],
            commit=False,
        )
        label = labels.get_or_create_label(conn, name=NOTEBOOK_LABEL, commit=False)
        if labels.add_label_to_page(conn, page["id"], label["id"], commit=False):
            page_activity.record_page_label_added(
                conn,
                actor_id=user_id,
                page_id=page["id"],
                label_id=label["id"],
                commit=False,
            )
        return page, True
