"""Operator command-line entry points for Athena."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from athena.core import db
from athena.core.backup import (
    backup_database,
    prune_backup_directory,
    restore_database,
    validate_retention_plan,
)
from athena.core.portability import (
    ATTACHMENT_POLICIES,
    dry_run_import_database,
    export_database,
    import_manifest_database,
    write_import_manifest_database,
)
from athena.core.run_replay import export_run_replay_database
from athena.core.source_import import SOURCE_KINDS, write_source_bundle


def _required_migrations() -> list[str]:
    return [path.name for path in sorted(db.MIGRATIONS_DIR.glob("*.sql"))]


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except sqlite3.Error as exc:
        raise ValueError(
            "database is not migrated: schema_migrations is missing"
        ) from exc
    return {row["version"] for row in rows}


def _format_missing_migrations(missing: list[str]) -> str:
    shown = ", ".join(missing[:5])
    if len(missing) > 5:
        shown = f"{shown}, ... ({len(missing)} total)"
    return shown


def _check_integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        reason = "no result" if result is None else result[0]
        raise ValueError(f"database integrity check failed: {reason}")


def _check_database(db_path: Path, *, migrate: bool) -> str:
    required = _required_migrations()
    if not required:
        raise ValueError("no Athena migrations were found")

    if migrate:
        conn = db.connect(db_path)
        try:
            applied_now = db.migrate(conn)
            _check_integrity(conn)
            applied = _applied_migrations(conn)
        finally:
            conn.close()
    else:
        if not db_path.exists():
            raise FileNotFoundError(f"database does not exist: {db_path}")
        uri = f"file:{db_path}?mode=rw"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            _check_integrity(conn)
            applied = _applied_migrations(conn)
        finally:
            conn.close()
        applied_now = []

    missing = [version for version in required if version not in applied]
    if missing:
        raise ValueError(
            f"database is missing migrations: {_format_missing_migrations(missing)}"
        )

    action = (
        f"applied {len(applied_now)} migrations" if applied_now else "already current"
    )
    return f"database: ok ({len(applied)} migrations, latest {required[-1]}, {action})"


def _check_attachment_dir(attach_dir: Path) -> str:
    if not attach_dir.exists():
        raise FileNotFoundError(f"attachment directory does not exist: {attach_dir}")
    if not attach_dir.is_dir():
        raise NotADirectoryError(f"attachment path is not a directory: {attach_dir}")

    with tempfile.NamedTemporaryFile(
        dir=attach_dir,
        prefix=".athena-doctor-",
        delete=True,
    ) as probe:
        probe.write(b"ok")
        probe.flush()

    return f"attachments: ok ({attach_dir})"


def doctor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-doctor",
        description="Check Athena deployment prerequisites.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument(
        "--attach-dir",
        type=Path,
        help="attachment directory to verify for writable blob storage",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="create or migrate the database before checking it",
    )
    args = parser.parse_args(argv)

    try:
        checks = [_check_database(args.db_path, migrate=args.migrate)]
        if args.attach_dir is not None:
            checks.append(_check_attachment_dir(args.attach_dir))
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-doctor: {exc}", file=sys.stderr)
        return 1

    for check in checks:
        print(check)
    print("athena-doctor: ok")
    return 0


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
    parser.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help=(
            "after a successful backup, keep the newest N backups matching the "
            "retention glob in the destination directory"
        ),
    )
    parser.add_argument(
        "--retention-glob",
        help=("file-name glob used with --keep; defaults to <source-db-stem>-*.db"),
    )
    args = parser.parse_args(argv)
    if args.keep is None and args.retention_glob is not None:
        print("athena-backup: --retention-glob requires --keep", file=sys.stderr)
        return 1
    retention_glob = args.retention_glob or f"{args.db_path.stem}-*.db"

    try:
        if args.keep is not None:
            validate_retention_plan(
                args.backup_path,
                retention_glob,
                keep=args.keep,
            )
        backup = backup_database(
            args.db_path,
            args.backup_path,
            overwrite=args.overwrite,
        )
        pruned = []
        if args.keep is not None:
            pruned = prune_backup_directory(
                backup.parent,
                retention_glob,
                keep=args.keep,
                protected=(backup,),
            )
    except (
        FileNotFoundError,
        FileExistsError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-backup: {exc}", file=sys.stderr)
        return 1

    print(f"Backed up {args.db_path} to {backup}")
    if args.keep is not None:
        print(
            "Pruned "
            f"{len(pruned)} old backup(s) matching {retention_glob!r}; "
            f"kept newest {args.keep}"
        )
    return 0


def export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-export",
        description="Export one Athena project or space to a portable JSON bundle.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument(
        "kind",
        choices=("project", "space"),
        help="container kind to export",
    )
    parser.add_argument("target_id", type=int, help="project or space id to export")
    parser.add_argument("bundle_path", type=Path, help="Destination JSON bundle path")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing bundle path",
    )
    args = parser.parse_args(argv)

    try:
        bundle = export_database(
            args.db_path,
            args.kind,
            args.target_id,
            args.bundle_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-export: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {args.kind} {args.target_id} from {args.db_path} to {bundle}")
    return 0


def export_run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-export-run",
        description="Export one tagged Athena run to a portable replay JSON artifact.",
    )
    parser.add_argument("db_path", type=Path, help="Athena SQLite database path")
    parser.add_argument("run_id", help="run id to export")
    parser.add_argument(
        "artifact_path", type=Path, help="Destination JSON artifact path"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing artifact path",
    )
    args = parser.parse_args(argv)

    try:
        artifact = export_run_replay_database(
            args.db_path,
            args.run_id,
            args.artifact_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-export-run: {exc}", file=sys.stderr)
        return 1

    print(f"Exported run {args.run_id} from {args.db_path} to {artifact}")
    return 0


def map_source_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-map-source",
        description="Map a supported source export into an Athena portability bundle.",
    )
    parser.add_argument("source", choices=SOURCE_KINDS, help="source export shape")
    parser.add_argument("source_path", type=Path, help="Source JSON export path")
    parser.add_argument("bundle_path", type=Path, help="Destination Athena bundle path")
    parser.add_argument("--project-key", help="override Jira project key")
    parser.add_argument("--project-name", help="override Jira project name")
    parser.add_argument("--space-key", help="override Confluence space key")
    parser.add_argument("--space-name", help="override Confluence space name")
    parser.add_argument(
        "--report-path",
        type=Path,
        help="optional destination for the source mapping report JSON",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing bundle path",
    )
    args = parser.parse_args(argv)

    try:
        bundle = write_source_bundle(
            args.source,
            args.source_path,
            args.bundle_path,
            project_key=args.project_key,
            project_name=args.project_name,
            space_key=args.space_key,
            space_name=args.space_name,
            report_path=args.report_path,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        ValueError,
    ) as exc:
        print(f"athena-map-source: {exc}", file=sys.stderr)
        return 1

    print(f"Mapped {args.source} export from {args.source_path} to {bundle}")
    if args.report_path is not None:
        print(f"Wrote source mapping report to {args.report_path}")
    return 0


def import_dry_run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-import-dry-run",
        description="Validate an Athena portability bundle without importing it.",
    )
    parser.add_argument("db_path", type=Path, help="Target Athena SQLite database path")
    parser.add_argument("bundle_path", type=Path, help="Source JSON bundle path")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full dry-run report as JSON",
    )
    args = parser.parse_args(argv)

    try:
        report = dry_run_import_database(args.db_path, args.bundle_path)
    except (
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-import-dry-run: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_import_dry_run_report(report)
    return 0 if report["ok"] else 1


def _print_import_dry_run_report(report: dict) -> None:
    print(f"athena-import-dry-run: {report['status']}")
    print(f"bundle: {report['kind']} {report['root_id']} ({report['schema']})")
    _print_count_block("would create", report["would_create"])
    _print_count_block("would reuse", report["would_reuse"])
    if report["conflicts"]:
        print("conflicts:")
        for item in report["conflicts"]:
            print(f"  - {item['code']}: {item['message']}")
    if report["warnings"]:
        print("warnings:")
        for item in report["warnings"]:
            print(f"  - {item['code']}: {item['message']}")


def _print_count_block(title: str, counts: dict[str, int]) -> None:
    print(f"{title}:")
    if not counts:
        print("  none")
        return
    for key, value in counts.items():
        print(f"  {key}: {value}")


def import_manifest_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-import-manifest",
        description="Build a selective import replay manifest without importing it.",
    )
    parser.add_argument("db_path", type=Path, help="Target Athena SQLite database path")
    parser.add_argument("bundle_path", type=Path, help="Source JSON bundle path")
    parser.add_argument("manifest_path", type=Path, help="Destination manifest path")
    parser.add_argument(
        "--attachment-policy",
        choices=ATTACHMENT_POLICIES,
        default="skip",
        help="how a future replay should handle attachment manifests",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing manifest path",
    )
    args = parser.parse_args(argv)

    try:
        manifest = write_import_manifest_database(
            args.db_path,
            args.bundle_path,
            args.manifest_path,
            attachment_policy=args.attachment_policy,
            overwrite=args.overwrite,
        )
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-import-manifest: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote import manifest to {args.manifest_path}: {manifest['status']}")
    return 0 if manifest["ok"] else 1


def import_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-import",
        description="Replay a ready Athena selective import manifest.",
    )
    parser.add_argument("db_path", type=Path, help="Target Athena SQLite database path")
    parser.add_argument("bundle_path", type=Path, help="Source JSON bundle path")
    parser.add_argument("manifest_path", type=Path, help="Ready replay manifest path")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full import result as JSON",
    )
    args = parser.parse_args(argv)

    try:
        result = import_manifest_database(
            args.db_path,
            args.bundle_path,
            args.manifest_path,
        )
    except (
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-import: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_import_result(result)
    return 0


def _print_import_result(result: dict) -> None:
    print(f"athena-import: {result['status']}")
    print(f"bundle: {result['kind']} {result['root_id']} ({result['schema']})")
    _print_count_block("created", result["created"])
    _print_count_block("reused", result["reused"])
    _print_count_block("skipped", result["skipped"])
    if result["warnings"]:
        print("warnings:")
        for item in result["warnings"]:
            print(f"  - {item['code']}: {item['message']}")


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
