"""Tiny cross-module helpers that had no home of their own.

The atomic-JSON-file writer and the UTC clock below were each copy-pasted, byte for
byte, across several core modules (portability / source_import / run_replay / sessions /
webhooks). One definition here, imported everywhere, so the copies can't silently drift.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def atomic_write_json(destination: Path, data: dict) -> None:
    """Write `data` as pretty, key-sorted JSON to `destination` atomically: serialize to a
    sibling temp file, then replace it into place, so a reader never sees a half-written
    file and a crash mid-write leaves the original intact. Was `_write_json_file`, defined
    identically in portability / source_import / run_replay."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def utc_now() -> datetime:
    """The current UTC instant as an aware datetime. Was `_now()` in sessions / webhooks."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """The current UTC instant as a second-precision ISO-8601 string ending in 'Z' (e.g.
    `2026-07-12T21:30:00Z`). Was `_utc_now()` in portability / source_import.

    NOTE: run_replay keeps its own `_utc_now` that emits `+00:00` rather than `Z` — a
    deliberate output-format difference, so it is intentionally NOT consolidated here."""
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
