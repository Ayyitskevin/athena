"""Read-time check of a completion report's evidence against attachments Athena holds.

Validation of a report is shape only. This module adds the one comparison Athena can honestly
make: an evidence item that names an attachment of the SAME issue and carries a digest is compared
with the digest Athena stored when it received those bytes. Everything else (a file in a checkout,
a forge event, a digest on a kind Athena cannot read) is only ever an unverified reference.

Nothing here refuses, writes, or changes the report; a mismatch is shown, not rejected. Only the
issue's own attachments are consulted, because a digest check against another container would
confirm that a hidden file exists.
"""

from __future__ import annotations

import sqlite3

MATCHES = "matches stored attachment"
UNVERIFIED = "unverified reference"

_NEW_KINDS = {"attachment", "forge_event"}
_NEW_FIELDS = {"digest", "bytes", "locator"}


def _asks_for_a_check(item: dict) -> bool:
    return item["kind"] in _NEW_KINDS or bool(_NEW_FIELDS & set(item))


def evidence_checks(
    conn: sqlite3.Connection, issue_id: int, report: dict | None
) -> list[str] | None:
    """One label per evidence item, or ``None`` when the report asks for no check at all.

    ``None`` keeps every old-shape report byte-for-byte what it was.
    """
    if not report or not any(
        _asks_for_a_check(item) for item in report.get("evidence", [])
    ):
        return None
    labels: list[str] = []
    for item in report["evidence"]:
        labels.append(_label(conn, issue_id, item))
    return labels


def _label(conn: sqlite3.Connection, issue_id: int, item: dict) -> str:
    if item["kind"] != "attachment" or "digest" not in item:
        return UNVERIFIED
    row = conn.execute(
        "SELECT sha256, byte_size FROM attachments "
        "WHERE id = ? AND target_kind = 'issue' AND target_id = ?",
        (int(item["ref"]), issue_id),
    ).fetchone()
    if row is None or row["sha256"] != item["digest"]:
        return UNVERIFIED
    if "bytes" in item and row["byte_size"] != item["bytes"]:
        return UNVERIFIED
    return MATCHES
