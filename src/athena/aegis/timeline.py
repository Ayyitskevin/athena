"""The project timeline — sprints as lanes, issues placed in them, edges drawn.

This is the roadmap view: *what is scheduled when, and what is waiting on what*,
for one project, without a workflow engine and without a scheduler. It answers
the question a tracker's roadmap answers and stops there.

**Coordinates, not markup.** Like ``core/graph.py``, this module returns
positioned lanes, cards and edges; the template places what it was given. That
keeps the geometry unit-testable without parsing XML and lets a future REST
caller hand an agent the same structure the browser draws.

**Lanes are ordinal, not a proportional date axis.** Sprint dates exist but are
nullable and unvalidated — a planned sprint legitimately has none, and nothing
stops a hand-set end before its start. Scaling lane widths by those dates would
collapse a project whose sprints are undated and misdraw one whose dates are
wrong, so lanes are equal columns in chronological ORDER and each prints its own
dates as a label. The order is a real reading of time; the width is not a claim
about duration.

**The picture never lies about what it left out.** Lanes are bounded per lane
and overall; ``shown``/``total``/``truncated`` report the difference, and
dependencies with one end outside the drawn set are counted separately rather
than silently dropped, because an arrow into empty space reads as a bug and a
missing arrow reads as "no dependency".

**No topological sort.** Only the direct two-cycle is refused at write time, so
A→B→C→A is storable today. Placement comes from sprint membership alone, so a
cycle changes the arrows and never the layout — nothing here can loop.
"""

from __future__ import annotations

import sqlite3

from athena.aegis import dependencies, issues, sprints, statuses

#: How many issues one lane draws before it starts reporting a remainder.
DEFAULT_MAX_PER_LANE = 12
MAX_MAX_PER_LANE = 40
#: The ceiling across the whole picture, so a huge project cannot render a page
#: of thousands of boxes.
DEFAULT_MAX_ITEMS = 120
MAX_MAX_ITEMS = 400

# Geometry. Pure layout constants; every value here is a pixel in the SVG's own
# coordinate space, which the template scales to the viewport.
LANE_WIDTH = 190.0
LANE_GAP = 26.0
#: How far a card sits inside its lane on each side. The card's own x/width are
#: emitted with this already applied, so the template places what it is given and
#: edge endpoints cannot drift from the boxes they connect.
CARD_INSET = 8.0
CARD_HEIGHT = 34.0
CARD_GAP = 10.0
HEADER_HEIGHT = 64.0
PAD = 18.0

#: The lane that holds this project's unscheduled issues. It sorts last: the
#: backlog is where work sits when it is not on the timeline yet.
BACKLOG_LANE = "backlog"


def _lane_sort_key(sprint: dict) -> tuple:
    """Chronological where dates exist, stable everywhere else.

    A sprint with a start date sorts by it; one with only an end date sorts by
    that; one with neither falls back to creation order (its id). Sprints are
    grouped so dated ones lead undated ones rather than interleaving on a
    fabricated date, and the id tiebreak keeps the layout reproducible.
    """
    start = (sprint.get("start_date") or "").strip()
    end = (sprint.get("end_date") or "").strip()
    if start:
        return (0, start, sprint["id"])
    if end:
        return (0, end, sprint["id"])
    return (1, "", sprint["id"])


def _bounded(value: object, *, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(value, maximum))


def project_timeline(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    visible_project_ids: set[int] | None = None,
    max_per_lane: int = DEFAULT_MAX_PER_LANE,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict:
    """One project's sprints as lanes, its issues placed in them, laid out.

    Returns ``{lanes, cards, edges, shown, total, truncated, edges_outside,
    width, height}``. ``lanes`` carry their own ``x``/``label``/``dates``/
    ``state``/``shown``/``total``; ``cards`` carry ``x``/``y`` plus the issue
    summary each one renders; ``edges`` carry precomputed endpoints and their
    ``kind`` so the template draws an arrow for a block and a plain line for a
    relation.

    The drawn issues are ``cards`` rather than ``items`` deliberately: Jinja
    resolves ``.items`` on a dict to the built-in method, so that name would hand
    a template a bound method instead of the drawing.

    Issues are gated by ``visible_project_ids`` exactly as every other list read
    is. That gate is about the ISSUES, not the project: the caller has already
    decided the viewer may see this project.
    """
    per_lane = _bounded(
        max_per_lane, default=DEFAULT_MAX_PER_LANE, maximum=MAX_MAX_PER_LANE
    )
    ceiling = _bounded(max_items, default=DEFAULT_MAX_ITEMS, maximum=MAX_MAX_ITEMS)

    lane_specs: list[dict] = [
        {
            "key": str(sprint["id"]),
            "sprint_id": sprint["id"],
            "label": sprint["name"],
            "state": sprint["state"],
            "start_date": sprint["start_date"],
            "end_date": sprint["end_date"],
        }
        for sprint in sorted(
            sprints.list_sprints(conn, project_id=project_id), key=_lane_sort_key
        )
    ]
    # The backlog lane always exists: "nothing is unscheduled" is a fact worth
    # showing, and an absent lane would read as a rendering gap instead.
    lane_specs.append(
        {
            "key": BACKLOG_LANE,
            "sprint_id": None,
            "label": "Backlog",
            "state": "",
            "start_date": None,
            "end_date": None,
        }
    )

    # One read for the whole project, partitioned into lanes in Python. A query
    # per lane would be N+1 across a project's sprint history, and `list_issues`
    # has no "in this project but in no sprint" filter — an issue always carries
    # its own sprint_id, so the partition is exact. Archived issues are excluded
    # by list_issues' default: the timeline shows work that is still live.
    by_lane: dict[str, list[dict]] = {spec["key"]: [] for spec in lane_specs}
    for issue in issues.list_issues(
        conn, project_id=project_id, visible_project_ids=visible_project_ids
    ):
        lane_key = (
            BACKLOG_LANE if issue["sprint_id"] is None else str(issue["sprint_id"])
        )
        # A sprint outside this project's lane list cannot happen today (sprints
        # belong to one project, and moving projects clears the sprint). If it
        # ever did, the issue lands in the backlog lane rather than in an orphan
        # bucket nothing iterates — which would drop it from the drawing AND from
        # the totals, making the picture quietly incomplete.
        by_lane.setdefault(lane_key, by_lane[BACKLOG_LANE]).append(issue)

    status_menus: dict[int | None, list[dict]] = {}
    lanes: list[dict] = []
    cards: list[dict] = []
    total = 0
    remaining = ceiling

    for index, spec in enumerate(lane_specs):
        lane_x = PAD + index * (LANE_WIDTH + LANE_GAP)
        rows = by_lane.get(spec["key"], [])
        total += len(rows)
        room = min(per_lane, max(0, remaining))
        drawn = rows[:room]
        remaining -= len(drawn)

        for row_index, issue in enumerate(drawn):
            project = issue.get("project_id")
            if project not in status_menus:
                # One status lookup per project rather than one per issue: a lane
                # of N issues in one project costs one query, not N.
                status_menus[project] = statuses.list_statuses(conn, project)
            category = next(
                (
                    entry["category"]
                    for entry in status_menus[project]
                    if entry["name"] == issue["status"]
                ),
                "doing",
            )
            cards.append(
                {
                    "id": issue["id"],
                    "width": round(LANE_WIDTH - 2 * CARD_INSET, 2),
                    "height": CARD_HEIGHT,
                    "key": issue["key"],
                    "title": issue["title"],
                    "status": issue["status"],
                    "category": category,
                    "lane": spec["key"],
                    "x": round(lane_x + CARD_INSET, 2),
                    "y": round(HEADER_HEIGHT + row_index * (CARD_HEIGHT + CARD_GAP), 2),
                }
            )
        lanes.append(
            {
                **spec,
                "x": round(lane_x, 2),
                "width": LANE_WIDTH,
                "shown": len(drawn),
                "total": len(rows),
                "hidden": len(rows) - len(drawn),
                "truncated": len(drawn) < len(rows),
            }
        )

    index_by_id = {card["id"]: position for position, card in enumerate(cards)}
    drawn_ids = list(index_by_id)
    edges: list[dict] = []
    for edge in dependencies.edges_among(conn, drawn_ids):
        source = cards[index_by_id[edge["from_id"]]]
        target = cards[index_by_id[edge["to_id"]]]
        edges.append(
            {
                "kind": edge["kind"],
                "from_id": edge["from_id"],
                "to_id": edge["to_id"],
                # Endpoints are the cards' right and left mid-edges — computed
                # from the same x/width the cards are drawn with, so an edge can
                # never land beside the box it points at.
                "x1": round(source["x"] + source["width"], 2),
                "y1": round(source["y"] + CARD_HEIGHT / 2, 2),
                "x2": round(target["x"], 2),
                "y2": round(target["y"] + CARD_HEIGHT / 2, 2),
            }
        )

    rows_needed = max((lane["shown"] for lane in lanes), default=0)
    width = PAD * 2 + len(lanes) * LANE_WIDTH + max(0, len(lanes) - 1) * LANE_GAP
    height = (
        HEADER_HEIGHT + rows_needed * (CARD_HEIGHT + CARD_GAP) + PAD
        if rows_needed
        else HEADER_HEIGHT + PAD
    )
    return {
        "lanes": lanes,
        "cards": cards,
        "edges": edges,
        "shown": len(cards),
        "total": total,
        "truncated": len(cards) < total,
        # Dependencies with one end off the picture. Counted, never drawn: an
        # arrow to nowhere reads as a bug, and no arrow at all reads as "nothing
        # blocks this".
        "edges_outside": dependencies.count_edges_touching(
            conn, drawn_ids, visible_project_ids=visible_project_ids
        ),
        "width": round(width, 2),
        "height": round(height, 2),
    }
