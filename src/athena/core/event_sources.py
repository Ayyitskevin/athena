"""Registered inbound event sources — who is allowed to tell Athena things.

The mirror image of ``webhooks``. That module is how Athena *pushes* events out;
this is how a forge *pushes* events in. Same trust boundary, opposite direction,
deliberately managed the same way: a registered row, a one-time secret, an
enable/disable switch, and an audited lifecycle.

The secret stored here is **Athena's**, not the forge's. It is the value an
inbound request must prove knowledge of. Athena holds no credential that would
let it call a forge, because it never does — inbound-only means no new egress
surface and no third-party token in the database to leak.

Data access only: the audited lifecycle lives in ``event_source_commands``, and
the landing rule that turns an authenticated delivery into issue history lives in
``aegis/forge`` (it needs to know what an issue is; this module does not).
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3

from athena.core import forge_events, webhooks

#: Raw secrets carry a short prefix so humans recognize them, matching the
#: convention API tokens and webhook secrets already use.
SECRET_PREFIX = "evtsec_"

#: Stand-in secret used to verify a delivery naming an UNKNOWN source, so the
#: refusal does the same HMAC work as a real check. Never stored; a match
#: against it still refuses (verify_signature requires a real row).
_UNKNOWN_SOURCE_SECRET = "unknown-source"


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def _public_row(row: sqlite3.Row) -> dict:
    """A source as the API returns it — **never** including the secret.

    The secret is returned exactly once, by the registration command, and is
    unreadable afterwards. A listing endpoint that echoed it would turn every
    admin read into a credential disclosure.
    """
    data = dict(row)
    data.pop("secret", None)
    data["enabled"] = bool(data["enabled"])
    return data


def create_source(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    host: str,
    created_by: int,
    commit: bool = True,
) -> dict:
    """Insert a source and return it **with** its one-time secret."""
    secret = generate_secret()
    cur = conn.execute(
        "INSERT INTO event_sources (name, kind, secret, host, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, kind, secret, host, created_by),
    )
    source_id = cur.lastrowid
    assert source_id is not None
    if commit:
        conn.commit()
    row = get_source(conn, source_id)
    assert row is not None
    return {**row, "secret": secret}


def get_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM event_sources WHERE id = ?", (source_id,)
    ).fetchone()
    return _public_row(row) if row else None


def get_source_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM event_sources WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return _public_row(row) if row else None


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM event_sources ORDER BY name").fetchall()
    return [_public_row(row) for row in rows]


def registered_hosts(conn: sqlite3.Connection) -> set[str]:
    """Every host Athena has been told to expect events from.

    Used to decide whether a reference in the trail may render as a link. A URL is
    made clickable only when its host belongs to a registered source, so a forge
    reference cannot be used to plant an arbitrary outbound link in the activity
    trail of a workspace that never registered that forge.
    """
    rows = conn.execute("SELECT host FROM event_sources").fetchall()
    return {row["host"].strip().lower() for row in rows if row["host"]}


def verify_signature(
    conn: sqlite3.Connection, source_id: int, body: bytes, provided: str | None
) -> bool:
    """Whether ``provided`` is the correct HMAC over exactly these bytes.

    Reads the secret directly rather than through ``get_source`` — the public row
    deliberately does not carry it. The comparison is constant-time, and a missing
    header is a mismatch rather than a special case, so an unsigned delivery and a
    wrongly-signed one are refused identically and take the same path.

    Two refusal-shape rules keep this endpoint from becoming a source-name oracle:

    * A malformed header (anything non-ASCII — a well-formed one is
      ``sha256=<hex>``) is a WRONG SIGNATURE, not an error: ``compare_digest``
      raises TypeError on non-ASCII str operands, which would turn the refusal
      into a 500 for a registered source while an unknown one refused cleanly.
    * An unknown source id still pays for one HMAC (against a stand-in secret
      whose comparison is discarded), so "registered" and "unregistered" do the
      same work on the way to the same 401.
    """
    if not provided:
        return False
    row = conn.execute(
        "SELECT secret FROM event_sources WHERE id = ?", (source_id,)
    ).fetchone()
    try:
        supplied = provided.encode("ascii")
    except UnicodeEncodeError:
        supplied = b""  # malformed: take the ordinary mismatch path below
    secret = row["secret"] if row is not None else _UNKNOWN_SOURCE_SECRET
    expected = webhooks.sign(secret, body).encode("ascii")
    matched = bool(supplied) and hmac.compare_digest(supplied, expected)
    # A match against the stand-in secret is not authentication: no real source,
    # no acceptance.
    return row is not None and matched


def record_delivery(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    matched: int,
    unmatched: int,
    commit: bool = True,
) -> None:
    """Update a source's health counters after one authenticated delivery.

    ``unmatched`` counts authentic events that named no issue this workspace
    knows. They are counted and not stored: landing them nowhere would be silent,
    and storing them somewhere would be noise on a trail that belongs to work.
    A climbing number is the only trace they leave, and it means something real —
    the forge is busy on work this workspace does not track.
    """
    conn.execute(
        "UPDATE event_sources SET accepted_count = accepted_count + ?, "
        "unmatched_count = unmatched_count + ?, last_event_at = datetime('now') "
        "WHERE id = ?",
        (matched, unmatched, source_id),
    )
    if commit:
        conn.commit()


def set_enabled(
    conn: sqlite3.Connection, source_id: int, enabled: bool, commit: bool = True
) -> dict | None:
    conn.execute(
        "UPDATE event_sources SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, source_id),
    )
    if commit:
        conn.commit()
    return get_source(conn, source_id)


def delete_source(
    conn: sqlite3.Connection, source_id: int, commit: bool = True
) -> bool:
    cur = conn.execute("DELETE FROM event_sources WHERE id = ?", (source_id,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


def valid_kind(kind: str) -> bool:
    return kind in forge_events.SOURCE_KINDS
