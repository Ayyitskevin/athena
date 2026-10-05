"""Shared browser presentation helpers.

List, board, filter, project, and mentor pages all need the same read-only
refusal, label chips, status dropdown, and integer query parse. They live
here so a route module does not reach into ``web.router`` for a private name.
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse

from athena.aegis import issues, statuses
from athena.core import labels

# Attachment-command refusal codes the browser adapters answer with.
ATTACHMENT_STATUS_BY_KIND = {"invalid": 422, "not_found": 404, "forbidden": 403}


def readonly_response() -> HTMLResponse:
    return HTMLResponse(
        '<div class="blocked">Viewer role is read-only.</div>',
        status_code=403,
    )


def attach_labels(conn, rows: list[dict]) -> list[dict]:
    """Merge each issue's labels onto it under a "labels" key, in one bulk query
    (no N+1). Mirrors the API's label composition so list/board cards can render
    their label chips. Issues with no labels get an empty list."""
    by_issue = labels.labels_for_issues(conn, [r["id"] for r in rows])
    for row in rows:
        row["labels"] = by_issue.get(row["id"], [])
    return rows


def statuses_in_use(conn, visible_project_ids: set[int] | None = None) -> list[str]:
    """The distinct statuses currently on any issue the viewer MAY SEE, ordered todo →
    doing → done then name. This is the option set BOTH status filters (the issue list
    and the board) offer, so a filter only ever lists statuses that really exist on
    visible issues — including a project's custom statuses — instead of a hardcoded
    open/in_progress/done trio. (Empty when there are none; the "All statuses" option
    always remains, so the control still renders.)

    visible_project_ids is what the caller gets from access.visible_project_filter: None
    means no gating (an admin's god view), a set restricts to those projects (plus the
    backlog). Without it a private project's CUSTOM status name would leak into the
    dropdown for someone who can't see that project (the rows are gated, but the option
    set was not).

    The distinct set comes from the data layer (one DISTINCT over the gated rows), not
    from collecting statuses off a full unpaged read — building a dropdown was the
    second unbounded fetch on every issue-list and board render."""
    cat_rank = {"todo": 0, "doing": 1, "done": 2}
    names = issues.statuses_in_use(conn, visible_project_ids=visible_project_ids)
    return sorted(
        names,
        key=lambda name: (cat_rank.get(statuses.global_category(conn, name), 1), name),
    )


def int_or_none(raw: str | None) -> int | None:
    """A query param parsed to int, or None if absent/blank/garbage — a filter the
    user can't set to a bad value just by editing the URL."""
    if raw is None or raw.strip() == "" or not raw.strip().lstrip("-").isdigit():
        return None
    return int(raw)
