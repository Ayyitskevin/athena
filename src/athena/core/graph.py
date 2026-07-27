"""The link graph, laid out — Athena's answer to the graph view.

``links`` already stores every edge between issues and pages. Backlinks answer
"what points at this?" one hop at a time; this module answers the question a
graph view is actually for: *what neighbourhood is this thing in?*

Three constraints shape the whole design, and each rules out the obvious
implementation:

  * **Bounded, always.** A global force-directed graph over an entire workspace
    is the feature everyone demos and nobody uses: it is slow, it is a hairball,
    and its cost grows with the database. This is an EGO graph — one focus, a
    bounded radius, a hard node ceiling — and when the ceiling truncates the
    neighbourhood it says so with a count, rather than drawing a partial picture
    that reads as complete.
  * **Deterministic.** Layout is pure arithmetic over a stable ordering, with no
    randomness and no iteration count — so no seed to fix, no "mostly stable"
    caveat. The same graph renders identically every time, which is what makes it
    diffable in a test and non-jarring to a human who reloads.
  * **Visibility is the viewer's.** Every node is gated with the same predicates
    as every other read, DURING traversal rather than after it. Filtering a
    finished graph would leak structure: a hidden node's edges would still shape
    the picture, and its absence at a known position would be an existence
    oracle. A node the viewer cannot see is not a node, and the walk does not
    continue through it.

Layout is emitted as coordinates, not markup: this module returns positioned
nodes and edges, and the template draws the SVG. That keeps the geometry
unit-testable without parsing XML, and lets REST hand an agent the same graph as
data rather than a picture it would have to scrape.

Like ``links`` and ``search``, this lives in core/ because it deliberately knows
about both issues and pages, and reads their tables directly rather than
importing aegis/mentor.
"""

from __future__ import annotations

import math
import sqlite3

from athena.core import access

# The kinds a graph node can be, and the table each lives in. Fixed literals,
# never caller input, so interpolating them into SQL is safe.
_TABLE = {"issue": "issues", "page": "pages"}

# Hops from the focus. 1 is "what touches this"; 2 is "and what touches those" —
# the radius where a neighbourhood is still legible. Beyond that an ego graph
# becomes the hairball this deliberately is not.
DEFAULT_DEPTH = 2
MAX_DEPTH = 3

# The node ceiling, including the focus. Chosen so the densest ring still renders
# with readable labels at the viewport widths Athena's pages use.
DEFAULT_MAX_NODES = 40
MAX_MAX_NODES = 120

# Geometry, in the abstract user units the SVG viewBox is expressed in.
_RING = 130.0  # radius added per depth level
_PAD = 70.0  # margin around the outermost ring, leaving room for labels


def _exists_and_visible(
    conn: sqlite3.Connection, actor: dict | None, kind: str, node_id: int
) -> bool:
    """Whether this node is a real row the actor may see.

    Both halves matter. ``links`` resolves lazily, so an edge may point at a
    target that never existed or has since been deleted; a graph of *things*
    should not sprout titleless ghosts. And visibility is checked here, at
    traversal time, so an invisible node neither renders nor conducts a path.
    """
    table = _TABLE.get(kind)
    if table is None:
        return False
    row = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return False
    if kind == "issue":
        return access.can_see_issue(conn, actor, node_id)
    return access.can_see_page(conn, actor, node_id)


def _neighbours(
    conn: sqlite3.Connection, kind: str, node_id: int
) -> list[tuple[str, int]]:
    """Every node one hop away, in either direction, deterministically ordered.

    The graph is drawn UNDIRECTED: for "what neighbourhood am I in", an inbound
    reference and an outbound one are the same adjacency. Direction is preserved
    on the edges themselves, so an arrowhead can still show which way the
    reference points.
    """
    rows = conn.execute(
        "SELECT target_kind AS k, target_id AS i FROM links "
        "WHERE source_kind = ? AND source_id = ? "
        "UNION "
        "SELECT source_kind AS k, source_id AS i FROM links "
        "WHERE target_kind = ? AND target_id = ? "
        "ORDER BY k, i",
        (kind, node_id, kind, node_id),
    ).fetchall()
    return [(r["k"], r["i"]) for r in rows]


def _titles(conn: sqlite3.Connection, kind: str, ids: list[int]) -> dict[int, dict]:
    """Titles and per-kind context for a batch of nodes, one query per kind."""
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    if kind == "issue":
        rows = conn.execute(
            f"SELECT i.id, i.title, i.status, i.project_seq, p.key AS project_key "
            f"FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
            f"WHERE i.id IN ({placeholders})",
            ids,
        ).fetchall()
        out = {}
        for r in rows:
            key = (
                f"{r['project_key']}-{r['project_seq']}"
                if r["project_key"] and r["project_seq"] is not None
                else None
            )
            out[r["id"]] = {"title": r["title"], "key": key, "status": r["status"]}
        return out
    rows = conn.execute(
        f"SELECT pg.id, pg.title, s.key AS space_key FROM pages pg "
        f"JOIN spaces s ON s.id = pg.space_id WHERE pg.id IN ({placeholders})",
        ids,
    ).fetchall()
    return {
        r["id"]: {"title": r["title"], "key": None, "space_key": r["space_key"]}
        for r in rows
    }


def ego_graph(
    conn: sqlite3.Connection,
    *,
    kind: str,
    node_id: int,
    actor: dict | None,
    depth: int = DEFAULT_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict:
    """The bounded, visibility-filtered neighbourhood around one node, laid out.

    Returns ``{focus, nodes, edges, shown, total, truncated, depth, width,
    height}``. Each node carries ``kind``, ``id``, ``title``, ``depth``, ``x``,
    ``y`` and its per-kind context; each edge carries ``source``/``target`` index
    pairs into ``nodes``.

    ``total`` counts every visible node discovered within ``depth``, whether or
    not it fit under ``max_nodes``; ``truncated`` says the picture is a window.
    Reporting the count the caller did NOT get is the difference between a
    bounded view and a quiet lie — an unlabelled partial graph reads as the whole
    neighbourhood.

    Discovery is breadth-first, so when the ceiling bites it keeps the CLOSEST
    nodes: the ones actually about the focus. Ties within a ring break on
    ``(kind, id)``, which is what makes the layout reproducible.
    """
    depth = max(1, min(depth, MAX_DEPTH))
    max_nodes = max(1, min(max_nodes, MAX_MAX_NODES))

    if not _exists_and_visible(conn, actor, kind, node_id):
        return {
            "focus": None,
            "nodes": [],
            "edges": [],
            "shown": 0,
            "total": 0,
            "truncated": False,
            "depth": depth,
            "width": 0.0,
            "height": 0.0,
        }

    # Breadth-first over visible nodes only. `levels[d]` is the ordered ring at
    # distance d; `seen` maps a node to the depth it was first reached at, which
    # is its true distance because BFS reaches every node by a shortest path.
    focus = (kind, node_id)
    seen: dict[tuple[str, int], int] = {focus: 0}
    levels: list[list[tuple[str, int]]] = [[focus]]
    for current in range(depth):
        ring: list[tuple[str, int]] = []
        for node in levels[current]:
            for neighbour in _neighbours(conn, node[0], node[1]):
                if neighbour in seen:
                    continue
                if not _exists_and_visible(conn, actor, neighbour[0], neighbour[1]):
                    continue  # invisible nodes neither render nor conduct
                seen[neighbour] = current + 1
                ring.append(neighbour)
        if not ring:
            break
        ring.sort()
        levels.append(ring)

    total = sum(len(ring) for ring in levels)

    # Apply the ceiling ring by ring, nearest first: a truncated graph keeps the
    # nodes closest to the focus rather than an arbitrary slice.
    kept: list[tuple[tuple[str, int], int]] = []
    for d, ring in enumerate(levels):
        for node in ring:
            if len(kept) >= max_nodes:
                break
            kept.append((node, d))
        if len(kept) >= max_nodes:
            break

    index = {node: i for i, (node, _) in enumerate(kept)}
    context = {
        k: _titles(conn, k, [n[1] for n, _ in kept if n[0] == k]) for k in _TABLE
    }

    # Concentric rings: depth d sits on radius d * _RING, evenly spaced. The
    # half-step offset on each ring keeps successive rings from lining their
    # nodes up on the same spokes, which is what makes edges legible without any
    # force simulation.
    per_depth: dict[int, list[tuple[str, int]]] = {}
    for node, d in kept:
        per_depth.setdefault(d, []).append(node)
    radius = _RING * max((d for _, d in kept), default=0)
    width = height = 2 * (radius + _PAD)
    centre = width / 2

    nodes: list[dict] = []
    for node, d in kept:
        ring = per_depth[d]
        if d == 0:
            x, y = centre, centre
        else:
            slot = ring.index(node)
            angle = 2 * math.pi * slot / len(ring) + (math.pi / len(ring)) * (d % 2)
            x = centre + _RING * d * math.cos(angle)
            y = centre + _RING * d * math.sin(angle)
        ctx = context[node[0]].get(node[1], {})
        nodes.append(
            {
                "kind": node[0],
                "id": node[1],
                "title": ctx.get("title") or "",
                "key": ctx.get("key"),
                "status": ctx.get("status"),
                "space_key": ctx.get("space_key"),
                "depth": d,
                "is_focus": node == focus,
                "x": round(x, 2),
                "y": round(y, 2),
            }
        )

    # Edges among KEPT nodes only. An edge to a node the ceiling cut would point
    # into empty space, which reads as a rendering bug rather than a boundary.
    edges: list[dict] = []
    for node, _ in kept:
        for neighbour in _neighbours(conn, node[0], node[1]):
            if neighbour not in index:
                continue
            a, b = index[node], index[neighbour]
            if a < b:  # each undirected pair once
                edges.append(
                    {
                        "source": a,
                        "target": b,
                        "x1": nodes[a]["x"],
                        "y1": nodes[a]["y"],
                        "x2": nodes[b]["x"],
                        "y2": nodes[b]["y"],
                    }
                )

    return {
        "focus": {"kind": kind, "id": node_id},
        "nodes": nodes,
        "edges": edges,
        "shown": len(nodes),
        "total": total,
        "truncated": len(nodes) < total,
        "depth": depth,
        "width": round(width, 2),
        "height": round(height, 2),
    }
