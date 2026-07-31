"""Operator command-line entry points for Athena."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import sys
import tempfile

from athena.core import attachments, db, deployment
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


def _sqlite_uri(path: Path, *, mode: str) -> str:
    """Build an exact SQLite URI without treating path bytes as URI syntax."""
    if mode not in {"ro", "rw"}:
        raise ValueError("SQLite URI mode must be ro or rw")
    return f"{path.absolute().as_uri()}?mode={mode}"


def _check_integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        reason = "no result" if result is None else result[0]
        raise ValueError(f"database integrity check failed: {reason}")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("database foreign key check failed")


def _application_schema(conn: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """Return the migration-owned logical schema, excluding its validated ledger."""
    return tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' AND name != 'schema_migrations' "
            "ORDER BY type, name"
        ).fetchall()
    )


def _check_application_schema(conn: sqlite3.Connection) -> None:
    expected = sqlite3.connect(":memory:")
    expected.row_factory = sqlite3.Row
    try:
        expected.execute("PRAGMA foreign_keys = ON")
        db.migrate(expected)
        expected_schema = _application_schema(expected)
    finally:
        expected.close()
    if _application_schema(conn) != expected_schema:
        raise ValueError("database schema does not match the packaged Athena schema")


def _dry_run_migration(db_path: Path) -> None:
    """Prove a migration and its final schema on a private copy first."""
    candidate = sqlite3.connect(":memory:")
    candidate.row_factory = sqlite3.Row
    try:
        if db_path.exists():
            source = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
            source.row_factory = sqlite3.Row
            try:
                source.backup(candidate)
            finally:
                source.close()
        candidate.execute("PRAGMA foreign_keys = ON")
        db.migrate(candidate)
        _check_integrity(candidate)
        db.migration_status(candidate)
        _check_application_schema(candidate)
    finally:
        candidate.close()


def _check_database(db_path: Path, *, migrate: bool) -> str:
    if migrate:
        # db.migrate() is forward-only and commits each migration. Rehearse the
        # complete operation and exact packaged schema on an in-memory backup so
        # an incompatible-but-recognizable database fails before the real file
        # receives even the first pending migration.
        _dry_run_migration(db_path)
        conn = db.connect(db_path)
        try:
            applied_now = db.migrate(conn)
            _check_integrity(conn)
            status = db.migration_status(conn)
            _check_application_schema(conn)
        finally:
            conn.close()
    else:
        if not db_path.exists():
            raise FileNotFoundError(f"database does not exist: {db_path}")
        conn = sqlite3.connect(_sqlite_uri(db_path, mode="rw"), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            _check_integrity(conn)
            status = db.migration_status(conn)
            _check_application_schema(conn)
        finally:
            conn.close()
        applied_now = []

    action = (
        f"applied {len(applied_now)} migrations" if applied_now else "already current"
    )
    return (
        f"database: ok ({len(status.applied)} migrations, "
        f"latest {status.latest}, {action})"
    )


def _check_attachment_dir(db_path: Path, attach_dir: Path) -> str:
    try:
        metadata = attach_dir.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"attachment directory does not exist: {attach_dir}"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"attachment path is not a directory: {attach_dir}")

    with tempfile.NamedTemporaryFile(
        dir=attach_dir,
        prefix=".athena-doctor-",
        delete=True,
    ) as probe:
        probe.write(b"ok")
        probe.flush()

    conn = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        blob_count = int(conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0])
        report = attachments.reconcile_storage(conn, attach_dir)
    finally:
        conn.close()

    if not report.ok:
        findings = {
            "missing": len(report.missing),
            "tampered": len(report.tampered),
            "size_mismatched": len(report.size_mismatched),
            "non_regular": len(report.non_regular),
            "unreadable": len(report.unreadable),
            "orphan_files": len(report.orphan_files),
        }
        detail = ", ".join(
            f"{name}={count}" for name, count in findings.items() if count
        )
        if report.storage_root_problem is not None:
            detail = (
                f"{detail}, " if detail else ""
            ) + f"storage_root={report.storage_root_problem}"
        raise ValueError(f"attachment integrity check failed: {detail}")

    return f"attachments: ok ({blob_count} blobs reconciled; {attach_dir})"


def _check_database_posture(
    db_path: Path,
    *,
    bootstrap: bool,
    enabled_oidc_issuer: str | None,
    max_request_body_bytes: int,
) -> str:
    conn = db.connect(db_path)
    try:
        return deployment.check_database_posture(
            conn,
            bootstrap=bootstrap,
            enabled_oidc_issuer=enabled_oidc_issuer,
            max_request_body_bytes=max_request_body_bytes,
        )
    finally:
        conn.close()


def _check_bootstrap_database_before_migration(db_path: Path) -> None:
    """Recognize an empty or existing Athena database without writing to it.

    A missing or structurally empty database is the normal bootstrap case. An
    existing database with objects must prove it is Athena through a valid
    immutable migration prefix and its stable users table. This prevents a path
    typo from migrating an unrelated SQLite database. The authoritative current
    schema and zero-user checks still run after migration.
    """
    if not db_path.exists():
        return
    conn = sqlite3.connect(_sqlite_uri(db_path, mode="ro"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        objects = conn.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        if not objects:
            return
        object_names = {str(row["name"]) for row in objects}
        if "schema_migrations" not in object_names:
            raise ValueError(
                "bootstrap refuses an existing database that is not recognized "
                "as Athena"
            )
        status = db.migration_status(conn, require_current=False)
        if not status.applied:
            # db.migrate() deliberately commits the canonical ledger before it
            # applies 0001. A power loss in that narrow window must remain
            # retryable, but only when the ledger is the database's sole
            # non-SQLite object and its schema passed migration_status().
            if object_names == {"schema_migrations"}:
                return
            raise ValueError(
                "bootstrap refuses an existing database without an applied "
                "Athena schema"
            )
        if "users" not in object_names:
            raise ValueError(
                "bootstrap refuses an existing database without an applied "
                "Athena schema"
            )
        if int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]) != 0:
            raise ValueError("bootstrap startup requires a database with zero users")
    finally:
        conn.close()


def _deployment_checks(
    db_path: Path,
    attach_dir: Path,
    *,
    bootstrap: bool,
    enabled_oidc_issuer: str | None,
    max_request_body_bytes: int,
) -> list[str]:
    if bootstrap:
        _check_bootstrap_database_before_migration(db_path)
    return [
        _check_database(db_path, migrate=bootstrap),
        _check_attachment_dir(db_path, attach_dir),
        _check_database_posture(
            db_path,
            bootstrap=bootstrap,
            enabled_oidc_issuer=enabled_oidc_issuer,
            max_request_body_bytes=max_request_body_bytes,
        ),
    ]


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _listener_address(fd: int) -> tuple[str, int]:
    if fd < 0:
        raise ValueError("listener file descriptor must be non-negative")
    try:
        duplicate = os.dup(fd)
    except OverflowError as exc:
        raise ValueError("listener file descriptor is out of range") from exc
    try:
        listener = socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise
    try:
        if listener.family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("listener file descriptor must be an IPv4 or IPv6 socket")
        if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError("listener file descriptor must be a stream socket")
        if listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1:
            raise ValueError("listener file descriptor must already be listening")
        address = listener.getsockname()
    finally:
        listener.close()
    if (
        not isinstance(address, tuple)
        or len(address) < 2
        or not isinstance(address[0], str)
        or not isinstance(address[1], int)
    ):
        raise ValueError("listener file descriptor has no numeric IP address")
    return address[0], address[1]


def _run_server(*, host: str, port: int, fd: int | None) -> None:
    import uvicorn

    from athena import config
    from athena.main import create_app

    application = create_app(
        network_mode=config.NETWORK_MODE,
        allowed_authorities=config.ALLOWED_AUTHORITIES,
        expected_server=(host, port),
    )
    if fd is not None:
        uvicorn.run(
            application,
            fd=fd,
            workers=1,
            reload=False,
            lifespan="on",
            proxy_headers=False,
            server_header=False,
            limit_concurrency=100,
            timeout_graceful_shutdown=30,
        )
        return
    uvicorn.run(
        application,
        host=host,
        port=port,
        workers=1,
        reload=False,
        lifespan="on",
        proxy_headers=False,
        server_header=False,
        limit_concurrency=100,
        timeout_graceful_shutdown=30,
    )


def serve_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="athena-serve",
        description="Start Athena through its supported fail-closed deployment gate.",
    )
    parser.add_argument(
        "--host",
        help="numeric bind address (defaults to 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port_argument,
        help="TCP port (defaults to 8000)",
    )
    parser.add_argument(
        "--fd",
        type=int,
        help="inherited listening IPv4/IPv6 socket descriptor",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="migrate an empty local database and enable first-admin bootstrap",
    )
    args = parser.parse_args(argv)
    if args.fd is not None and (args.host is not None or args.port is not None):
        parser.error("--fd cannot be combined with --host or --port")

    try:
        from athena import config

        if args.fd is None:
            bind_host, bind_port = deployment.validate_bind(
                args.host or "127.0.0.1",
                args.port or 8000,
                config.NETWORK_MODE,
            )
        else:
            inherited_host, inherited_port = _listener_address(args.fd)
            bind_host, bind_port = deployment.validate_bind(
                inherited_host,
                inherited_port,
                config.NETWORK_MODE,
            )
        authorities = deployment.validate_static_configuration(
            db_path=config.DB_PATH,
            attach_dir=config.ATTACH_DIR,
            network_mode=config.NETWORK_MODE,
            bind_host=bind_host,
            bind_port=bind_port,
            allowed_authorities=config.ALLOWED_AUTHORITIES,
            bootstrap=args.bootstrap,
            bootstrap_token=config.BOOTSTRAP_TOKEN,
            trust_actor_header=config.TRUST_ACTOR_HEADER,
            cookie_secure=config.COOKIE_SECURE,
            oidc_issuer=config.OIDC_ISSUER if config.oidc_enabled() else None,
            oidc_redirect_url=(
                config.OIDC_REDIRECT_URL if config.oidc_enabled() else None
            ),
            max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
            token_rate_limit_per_minute=config.TOKEN_RATE_LIMIT_PER_MINUTE,
            anon_rate_limit_per_minute=config.ANON_RATE_LIMIT_PER_MINUTE,
            login_rate_limit_per_minute=config.LOGIN_RATE_LIMIT_PER_MINUTE,
        )
        checks = _deployment_checks(
            config.DB_PATH,
            config.ATTACH_DIR,
            bootstrap=args.bootstrap,
            enabled_oidc_issuer=(config.OIDC_ISSUER if config.oidc_enabled() else None),
            max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
        )
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"athena-serve: {exc}", file=sys.stderr)
        return 1

    # The route layer reads these process-local bootstrap invariants. _run_server
    # then constructs a fresh app from the normalized policy and pins it to the
    # exact preflighted listener before handing the object to Uvicorn.
    config.ALLOWED_AUTHORITIES = tuple(authority.render() for authority in authorities)
    config.BOOTSTRAP_PASSWORD_REQUIRED = args.bootstrap
    for check in checks:
        print(check, flush=True)
    print(
        f"network: ok ({config.NETWORK_MODE}; {bind_host}:{bind_port}; "
        f"{len(authorities)} Host authorities)",
        flush=True,
    )
    print("athena-serve: preflight ok", flush=True)
    _run_server(
        host=bind_host,
        port=bind_port,
        fd=args.fd,
    )
    return 0


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
    parser.add_argument(
        "--deployment",
        action="store_true",
        help="apply normal athena-serve deployment and recovery checks",
    )
    parser.add_argument(
        "--host",
        help="numeric deployment bind address (defaults to 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=_port_argument,
        help="deployment TCP port (defaults to 8000)",
    )
    args = parser.parse_args(argv)
    if not args.deployment and (args.host is not None or args.port is not None):
        parser.error("--host and --port require --deployment")
    if args.deployment and args.migrate:
        parser.error("--deployment cannot be combined with --migrate")
    if args.deployment and args.attach_dir is None:
        parser.error("--deployment requires --attach-dir")

    try:
        if args.deployment:
            from athena import config

            assert args.attach_dir is not None
            bind_host, bind_port = deployment.validate_bind(
                args.host or "127.0.0.1",
                args.port or 8000,
                config.NETWORK_MODE,
            )
            authorities = deployment.validate_static_configuration(
                db_path=args.db_path,
                attach_dir=args.attach_dir,
                network_mode=config.NETWORK_MODE,
                bind_host=bind_host,
                bind_port=bind_port,
                allowed_authorities=config.ALLOWED_AUTHORITIES,
                bootstrap=False,
                bootstrap_token=config.BOOTSTRAP_TOKEN,
                trust_actor_header=config.TRUST_ACTOR_HEADER,
                cookie_secure=config.COOKIE_SECURE,
                oidc_issuer=config.OIDC_ISSUER if config.oidc_enabled() else None,
                oidc_redirect_url=(
                    config.OIDC_REDIRECT_URL if config.oidc_enabled() else None
                ),
                max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
                token_rate_limit_per_minute=config.TOKEN_RATE_LIMIT_PER_MINUTE,
                anon_rate_limit_per_minute=config.ANON_RATE_LIMIT_PER_MINUTE,
                login_rate_limit_per_minute=config.LOGIN_RATE_LIMIT_PER_MINUTE,
            )
            if args.db_path != config.DB_PATH:
                raise ValueError("deployment db_path must exactly match ATHENA_DB")
            if args.attach_dir != config.ATTACH_DIR:
                raise ValueError(
                    "deployment --attach-dir must exactly match ATHENA_ATTACH_DIR"
                )
            checks = _deployment_checks(
                args.db_path,
                args.attach_dir,
                bootstrap=False,
                enabled_oidc_issuer=(
                    config.OIDC_ISSUER if config.oidc_enabled() else None
                ),
                max_request_body_bytes=config.MAX_REQUEST_BODY_BYTES,
            )
            checks.append(
                f"network: ok ({config.NETWORK_MODE}; {bind_host}:{bind_port}; "
                f"{len(authorities)} Host authorities)"
            )
        else:
            checks = [_check_database(args.db_path, migrate=args.migrate)]
            if args.attach_dir is not None:
                checks.append(_check_attachment_dir(args.db_path, args.attach_dir))
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
