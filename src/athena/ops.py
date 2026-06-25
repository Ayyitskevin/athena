"""Operator command-line entry points for Athena."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

from athena.core.backup import backup_database, restore_database


def backup_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-backup",
        description="Back up an Athena SQLite database.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument("backup_path", type=Path, help="Destination backup path")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing backup path",
    )
    args = parser.parse_args(argv)

    try:
        backup = backup_database(
            args.db_path,
            args.backup_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-backup: {exc}", file=sys.stderr)
        return 1

    print(f"Backed up {args.db_path} to {backup}")
    return 0


def restore_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-restore",
        description="Restore an Athena SQLite database backup.",
    )
    parser.add_argument("backup_path", type=Path, help="Source backup path")
    parser.add_argument("db_path", type=Path, help="Target Athena database path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing target database",
    )
    args = parser.parse_args(argv)

    try:
        restored = restore_database(
            args.backup_path,
            args.db_path,
            force=args.force,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-restore: {exc}", file=sys.stderr)
        return 1

    print(f"Restored {args.backup_path} to {restored}")
    return 0
