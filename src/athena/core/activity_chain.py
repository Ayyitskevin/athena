"""The activity trail's hash chain: append with each write, verify on demand.

The activity log is Athena's load-bearing promise, and until 0072 its
append-only nature was convention plus a handful of DB-frozen rows. This module
makes the trail *verifiable*: every row recorded after adoption gets a sidecar
``activity_chain`` entry whose ``entry_hash`` covers the row's stored facts plus
the previous entry's hash. Recomputing the chain therefore re-derives history —
an edited row, a vanished row, or an out-of-band insert breaks every hash after
it, and a bounded verify walk points at the first broken link.

Adapted from Buzz's "every action is a signed event in one log" — minus the
signatures, on purpose. Buzz members sign with their own keys; Athena's server
holds every credential, so a server-side signature would attest nothing a hash
does not. What transfers is verifiability without trusting the reader's memory.

The chain is honest about its edges (docs/TRAIL_INTEGRITY.md):

- Rows recorded before adoption are below the ANCHOR (the first chained row) and
  are reported as such, never folded into a verified claim.
- A writer who can rewrite both tables can rebuild the whole chain; truncating
  the newest rows of both tables together leaves a shorter but self-consistent
  chain. The HEAD hash exists so an operator can note it outside Athena and make
  even those detectable.

``append_entry`` is called from exactly one place — ``activity.record``, inside
the caller's write transaction — so the head it links to cannot move underneath
it (SQLite's single-writer reservation serializes appenders).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

SCHEMA = "athena.activity_chain.v1"
SCHEMA_VERSION = 1

#: prev_hash of the first chained row. A constant, not a secret: the chain's
#: strength is the linkage, and the genesis value is only accepted while the
#: chain is empty (the 0072 extends-head trigger).
GENESIS = "0" * 64

#: How many rows one verify call examines by default, and at most. Bounded so a
#: verify request is always cheap; the cursor makes the full walk a loop of
#: bounded calls (the doctor CLI does exactly that).
DEFAULT_VERIFY_LIMIT = 1000
MAX_VERIFY_LIMIT = 5000

# The stored columns the hash covers — everything record() writes, nothing a
# later read joins on (actor display names are mutable presentation, so they
# must never enter the recipe).
_ROW_SQL = (
    "SELECT id, actor_id, verb, target_kind, target_id, detail, created_at, "
    "run_id, parent_run_id, forked_from_event_id, visibility_restricted, "
    "reverses_event_id, imported_at FROM activity"
)


def entry_hash(row: sqlite3.Row | dict, prev_hash: str) -> str:
    """The SHA-256 (hex) of one activity row's canonical serialization.

    The recipe is part of the schema (``schema_version`` pins it): canonical
    JSON — sorted keys, compact separators, ASCII escapes — over the stored
    columns plus ``prev_hash`` and the recipe version. Deterministic across
    platforms and Python versions, so a verifier recomputes byte-identical
    input.
    """
    payload = {
        "v": SCHEMA_VERSION,
        "prev": prev_hash,
        "id": row["id"],
        "actor_id": row["actor_id"],
        "verb": row["verb"],
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "detail": row["detail"],
        "created_at": row["created_at"],
        "run_id": row["run_id"],
        "parent_run_id": row["parent_run_id"],
        "forked_from_event_id": row["forked_from_event_id"],
        "visibility_restricted": row["visibility_restricted"],
        "reverses_event_id": row["reverses_event_id"],
        "imported_at": row["imported_at"],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def head(conn: sqlite3.Connection) -> dict | None:
    """The chain's newest entry, or None while the chain is empty."""
    row = conn.execute(
        "SELECT event_id, entry_hash FROM activity_chain ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else dict(row)


def anchor_event_id(conn: sqlite3.Connection) -> int | None:
    """The first chained event id — where the chain's coverage begins."""
    row = conn.execute("SELECT MIN(event_id) AS a FROM activity_chain").fetchone()
    return None if row is None or row["a"] is None else int(row["a"])


def append_entry(conn: sqlite3.Connection, event_id: int) -> dict:
    """Chain one just-inserted activity row. Same transaction as the insert.

    Reads the row's STORED facts back (never trusting what the caller thinks it
    wrote), links to the current head, and appends. The 0072 triggers re-check
    the linkage shape, so even a bug here cannot append a side branch.
    """
    row = conn.execute(f"{_ROW_SQL} WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"activity row {event_id} does not exist")
    current = head(conn)
    prev = GENESIS if current is None else current["entry_hash"]
    digest = entry_hash(row, prev)
    conn.execute(
        "INSERT INTO activity_chain (event_id, prev_hash, entry_hash) VALUES (?, ?, ?)",
        (event_id, prev, digest),
    )
    return {"event_id": event_id, "prev_hash": prev, "entry_hash": digest}


def status(conn: sqlite3.Connection) -> dict:
    """Where the chain stands: anchor, head, and what it does/doesn't cover."""
    latest = conn.execute("SELECT MAX(id) AS m FROM activity").fetchone()
    latest_event_id = (
        None if latest is None or latest["m"] is None else int(latest["m"])
    )
    anchor = anchor_event_id(conn)
    chained = int(
        conn.execute("SELECT COUNT(*) AS n FROM activity_chain").fetchone()["n"]
    )
    if anchor is None:
        unchained = int(
            conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()["n"]
        )
    else:
        unchained = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM activity WHERE id < ?", (anchor,)
            ).fetchone()["n"]
        )
    return {
        "schema": SCHEMA,
        "anchor_event_id": anchor,
        "head": head(conn),
        "chained_count": chained,
        # Rows below the anchor: recorded before the chain existed, readable as
        # ever, but outside what verification can vouch for.
        "unchained_count": unchained,
        "latest_event_id": latest_event_id,
    }


def _mismatch(event_id: int, reason: str, detail: str) -> dict:
    return {"event_id": event_id, "reason": reason, "detail": detail}


def verify(
    conn: sqlite3.Connection,
    *,
    after_id: int | None = None,
    limit: int = DEFAULT_VERIFY_LIMIT,
) -> dict:
    """Recompute a bounded window of the chain and report the first break.

    Resumable: pass the returned ``next_after`` back in until ``has_more`` is
    false — each call does at most ``limit`` rows of work, so verification can
    never hold the database (or a request worker) for an unbounded walk. The
    walk stops AT a mismatch: everything after a broken link inherits the
    break, so reporting one exact spot beats reporting a thousand echoes.
    """
    bounded = min(max(int(limit), 1), MAX_VERIFY_LIMIT)
    anchor = anchor_event_id(conn)
    if anchor is None:
        # An empty chain verifies vacuously — there is nothing to claim yet.
        return {
            "schema": SCHEMA,
            "ok": True,
            "checked": 0,
            "after_id": after_id,
            "through_event_id": None,
            "next_after": None,
            "has_more": False,
            "mismatch": None,
            "anchor_event_id": None,
        }
    # Rows below the anchor are before the chain; verification starts at it.
    start_exclusive = anchor - 1 if after_id is None else max(after_id, anchor - 1)

    # The expected prev_hash for the first row in this window: genesis at the
    # anchor, otherwise the stored entry_hash of the row we resume after. Trust
    # is transitive only because the caller verified earlier windows first —
    # verify() states results per window and invents nothing beyond it.
    if start_exclusive == anchor - 1:
        expected_prev = GENESIS
    else:
        prior = conn.execute(
            "SELECT entry_hash FROM activity_chain WHERE event_id = ?",
            (start_exclusive,),
        ).fetchone()
        if prior is None:
            # Resuming after an id that is not a chain entry would silently
            # re-anchor the walk; refuse by reporting it as the finding.
            return {
                "schema": SCHEMA,
                "ok": False,
                "checked": 0,
                "after_id": after_id,
                "through_event_id": None,
                "next_after": None,
                "has_more": False,
                "mismatch": _mismatch(
                    start_exclusive,
                    "bad_cursor",
                    "next_after must be a previously verified chain entry",
                ),
                "anchor_event_id": anchor,
            }
        expected_prev = prior["entry_hash"]

    rows = conn.execute(
        f"{_ROW_SQL} WHERE id > ? ORDER BY id ASC LIMIT ?",
        (start_exclusive, bounded + 1),
    ).fetchall()
    has_more = len(rows) > bounded
    rows = rows[:bounded]

    # Chain entries in the same id window, keyed for the walk. Entries without
    # an activity row cannot exist (FK + the 0072 delete lock), so driving the
    # walk from activity finds every discrepancy the schema can express.
    if rows:
        chain_rows = conn.execute(
            "SELECT event_id, prev_hash, entry_hash FROM activity_chain "
            "WHERE event_id > ? AND event_id <= ? ORDER BY event_id ASC",
            (start_exclusive, rows[-1]["id"]),
        ).fetchall()
    else:
        chain_rows = []
    by_event = {entry["event_id"]: entry for entry in chain_rows}

    checked = 0
    through: int | None = None
    mismatch: dict | None = None
    for row in rows:
        entry = by_event.get(row["id"])
        if entry is None:
            mismatch = _mismatch(
                row["id"],
                "missing_chain_row",
                "activity row above the anchor has no chain entry",
            )
            break
        if entry["prev_hash"] != expected_prev:
            mismatch = _mismatch(
                row["id"],
                "prev_hash_mismatch",
                "chain entry does not link to the previous entry",
            )
            break
        recomputed = entry_hash(row, entry["prev_hash"])
        if entry["entry_hash"] != recomputed:
            mismatch = _mismatch(
                row["id"],
                "entry_hash_mismatch",
                "stored facts do not match the hash recorded for them",
            )
            break
        expected_prev = entry["entry_hash"]
        checked += 1
        through = row["id"]

    ok = mismatch is None
    return {
        "schema": SCHEMA,
        "ok": ok,
        "checked": checked,
        "after_id": after_id,
        "through_event_id": through,
        "next_after": through if (ok and has_more) else None,
        "has_more": has_more and ok,
        "mismatch": mismatch,
        "anchor_event_id": anchor,
    }


def verify_tail(conn: sqlite3.Connection, *, count: int = 200) -> dict:
    """Verify the newest ``count`` chain entries — the cheap freshness check a
    page render can afford. A clean tail is NOT a clean trail (the walk starts
    mid-chain and trusts the stored hash it resumes from); the full claim needs
    the whole walk (``verify_all`` / athena-doctor)."""
    bounded = min(max(int(count), 1), MAX_VERIFY_LIMIT)
    row = conn.execute(
        "SELECT event_id FROM activity_chain ORDER BY event_id DESC LIMIT 1 OFFSET ?",
        (bounded,),
    ).fetchone()
    after = None if row is None else int(row["event_id"])
    return verify(conn, after_id=after, limit=bounded)


def verify_all(conn: sqlite3.Connection, *, limit: int = DEFAULT_VERIFY_LIMIT) -> dict:
    """Walk the whole chain in bounded windows (the doctor CLI's full check).

    Returns the final window's report with ``checked`` accumulated across the
    walk, so a caller sees the total and, on failure, the exact first break.
    """
    total = 0
    after: int | None = None
    while True:
        report = verify(conn, after_id=after, limit=limit)
        total += report["checked"]
        if report["mismatch"] is not None or not report["has_more"]:
            report["checked"] = total
            return report
        after = report["next_after"]
