"""Tiny cross-module helpers that had no home of their own.

The atomic-JSON-file writer and the UTC clock below were each copy-pasted, byte for
byte, across several core modules (portability / source_import / run_replay / sessions /
webhooks). One definition here, imported everywhere, so the copies can't silently drift.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def atomic_write_json(destination: Path, data: dict) -> None:
    """Durably replace ``destination`` with private, deterministic JSON.

    Serialization finishes before a private unique sibling file is created. The
    staged bytes and containing directory are synced so a crash cannot expose a
    partial file or silently discard the rename.
    """
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        _fsync_directory(destination.parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def utc_now() -> datetime:
    """The current UTC instant as an aware datetime. Was `_now()` in sessions / webhooks."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """The current UTC instant as a second-precision ISO-8601 string ending in 'Z' (e.g.
    `2026-07-12T21:30:00Z`). Was `_utc_now()` in portability / source_import.

    NOTE: run_replay keeps its own `_utc_now` that emits `+00:00` rather than `Z` — a
    deliberate output-format difference, so it is intentionally NOT consolidated here."""
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
