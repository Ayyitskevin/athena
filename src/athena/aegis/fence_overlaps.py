"""Live leases whose declared file fences overlap: shown, never enforced.

The claim command refuses an overlapping fence when a lease is claimed
(``lease_commands._refuse_overlapping_paths``) and stays the only gate. Overlaps
can still exist afterwards — a root catalog that changed under live leases, rows
written outside the command — and nothing said so. This read finds them.

**What is compared.** Every lease the clock says is live, decoded by the lease
module's own reader, so paths are already normalised by the claim grammar:

- ``./`` and empty segments drop, a trailing ``/`` drops, ``\\`` becomes ``/``;
- a path containing ``..`` is refused by that grammar, so the whole stored
  fence is unreadable. It is never collapsed into another path, and it is
  counted as ``unreadable_fences`` rather than guessed at.

A path is compared within one base:

- **same root:** both fences resolve through the current root catalog to the
  same tree, and the compared path is that root's prefix joined to the declared
  path — the claim command's own canonical form;
- **lexical, root unknown:** both fences have readable paths but no verified
  root, so their relative paths are compared as written. This is a warning, not
  proof: two unknown roots may be different checkouts.

A rooted and an unknown-root fence have no common base and are not compared.
Issue-only leases (no paths) fence no files. Two paths overlap when they are
equal or one is a directory prefix of the other (the claim command's
``paths_overlap``): ``src/app`` covers ``src/app/models.py`` but not
``src/application``.

**Cost.** Each path is indexed once, and each path's own ancestors are looked
up in that index, so the work grows with the number of paths times their depth,
never with the square of the leases.

**Visibility.** Leases on issues the viewer cannot see are dropped before
pairing, so a hidden project's lease appears in no pair, no count and no office.
Read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from athena import config
from athena.aegis import leases
from athena.core import access, coordination_roots

#: Pairs listed per read; the total says how many exist behind the list.
PAIR_LIMIT = 50

BASIS_ROOTED = "same root"
BASIS_LEXICAL = "lexical, root unknown"


def _now(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _visible_ids(
    conn: sqlite3.Connection, issue_ids: list[int], actor: dict | None
) -> dict[int, dict]:
    """The leased issues this viewer may see, with their display fields.
    ``actor=None`` is the admin-only card's own caller contract (all)."""
    if not issue_ids:
        return {}
    placeholders = ",".join("?" for _ in issue_ids)
    clause, params = "", []
    if actor is not None:
        visible = access.visible_project_filter(conn, actor)
        if visible is not None:
            if visible:
                vis = ",".join("?" for _ in visible)
                clause = f" AND (i.project_id IS NULL OR i.project_id IN ({vis}))"
                params = sorted(visible)
            else:
                clause = " AND i.project_id IS NULL"
    rows = conn.execute(
        "SELECT i.id, i.title, i.project_seq, p.key AS project_key "
        "FROM issues i LEFT JOIN projects p ON p.id = i.project_id "
        f"WHERE i.id IN ({placeholders}){clause}",
        [*issue_ids, *params],
    ).fetchall()
    out = {}
    for row in rows:
        key = None
        if row["project_key"] is not None and row["project_seq"] is not None:
            key = f"{row['project_key']}-{row['project_seq']}"
        out[int(row["id"])] = {"id": int(row["id"]), "key": key, "title": row["title"]}
    return out


def _indexed_paths(
    lease: dict, catalog: coordination_roots.RootCatalog | None
) -> tuple[list[tuple[tuple[str, ...], str, str]], bool]:
    """``([(base, compared_path, declared_path)], unreadable)`` for one lease."""
    paths = list(lease.get("declared_paths") or [])
    fence = lease.get("path_fence")
    if fence == "issue_only":
        return [], False
    if not paths:
        # A non-empty stored fence the grammar refused (".." included).
        return [], True
    root = lease.get("coordination_root")
    if fence == "rooted" and root is not None:
        try:
            tree_id, prefix = coordination_roots.resolve(root, catalog)
        except coordination_roots.RootError:
            return [(("lexical",), path, path) for path in paths], False
        return [
            (("root", tree_id), "/".join(p for p in (prefix, path) if p), path)
            for path in paths
        ], False
    return [(("lexical",), path, path) for path in paths], False


def find_overlaps(
    conn: sqlite3.Connection,
    *,
    actor: dict | None,
    now: datetime | None = None,
    limit: int = PAIR_LIMIT,
) -> dict:
    """Pairs of live leases whose fences overlap, among issues ``actor`` can see.

    ``actor=None`` is the admin-only card's caller contract and sees all. Each
    pair names both issues, both holders, the first overlapping paths and the
    basis of the comparison. Read-only; refuses nothing."""
    current = _now(now)
    catalog = config.LEASE_ROOT_CATALOG
    live = leases.live_leases_at(conn, now=current)
    visible = _visible_ids(conn, [int(lease["issue_id"]) for lease in live], actor)
    live = [lease for lease in live if int(lease["issue_id"]) in visible]

    unreadable = 0
    entries: list[list[tuple[tuple[str, ...], str, str]]] = []
    index: dict[tuple[tuple[str, ...], str], list[tuple[int, str]]] = {}
    for position, lease in enumerate(live):
        indexed, bad = _indexed_paths(lease, catalog)
        unreadable += int(bad)
        entries.append(indexed)
        for base, compared, declared in indexed:
            index.setdefault((base, compared), []).append((position, declared))

    # For every path, look up each of its own ancestors (and itself) in the
    # index: a hit is another lease fencing an equal or enclosing path. Both
    # directions are found, because each side looks up its own ancestors.
    found: dict[tuple[int, int], tuple[str, str, tuple[str, ...]]] = {}
    for position, indexed in enumerate(entries):
        for base, compared, declared in indexed:
            parts = compared.split("/")
            for depth in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:depth])
                for other, other_declared in index.get((base, ancestor), ()):
                    if other == position:
                        continue
                    pair = (min(position, other), max(position, other))
                    if pair in found:
                        continue
                    mine, theirs = (
                        (declared, other_declared)
                        if position < other
                        else (other_declared, declared)
                    )
                    found[pair] = (mine, theirs, base)

    def side(position: int, path: str) -> dict:
        lease = live[position]
        return {
            "issue": visible[int(lease["issue_id"])],
            "holder": {"id": int(lease["holder_id"]), "name": lease["holder_name"]},
            "path": path,
        }

    ordered = sorted(
        found.items(),
        key=lambda kv: (
            int(live[kv[0][0]]["issue_id"]),
            int(live[kv[0][1]]["issue_id"]),
        ),
    )
    pairs = [
        {
            "a": side(a, path_a),
            "b": side(b, path_b),
            "basis": BASIS_ROOTED if base[0] == "root" else BASIS_LEXICAL,
        }
        for (a, b), (path_a, path_b, base) in ordered
    ]
    return {
        "pairs": pairs[:limit],
        "total": len(pairs),
        "limit": limit,
        "clipped": len(pairs) > limit,
        "examined_leases": len(live),
        "unreadable_fences": unreadable,
        "meaning": (
            "live leases whose declared paths overlap (equal, or one a directory "
            "prefix of the other), compared within one resolved root or lexically "
            "when the root is unknown; unreadable fences are counted, never "
            "guessed. Shown only: the claim command's overlap check stays the "
            "only gate"
        ),
    }


def overlaps_for_holder(
    conn: sqlite3.Connection, *, actor: dict, now: datetime | None = None
) -> list[dict]:
    """This holder's side of every visible overlap: the other issue and holder,
    and a one-line warning."""
    me = int(actor["id"])
    result = find_overlaps(conn, actor=actor, now=now, limit=10_000)
    out = []
    for pair in result["pairs"]:
        for mine, theirs in ((pair["a"], pair["b"]), (pair["b"], pair["a"])):
            if mine["holder"]["id"] != me or theirs["holder"]["id"] == me:
                continue
            label = theirs["issue"]["key"] or f"#{theirs['issue']['id']}"
            out.append(
                {
                    "your_issue": mine["issue"],
                    "your_path": mine["path"],
                    "issue": theirs["issue"],
                    "holder": theirs["holder"],
                    "their_path": theirs["path"],
                    "basis": pair["basis"],
                    "message": (
                        f"your fence overlaps issue {label}, held by "
                        f"{theirs['holder']['name']}"
                    ),
                }
            )
    return out
