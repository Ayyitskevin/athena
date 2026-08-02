"""Create a disposable, review-ready Athena workspace and serve it locally.

The demo is deliberately synthetic and fail-closed: the caller must choose a new
database path, existing files are never reset, and the server binds to loopback.
It seeds through Athena's real data and command layers so the resulting project,
docs, cross-links, agent activity, and run metadata are useful in a code review.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from athena import config
from athena import guide as field_guide_content
from athena.aegis import issue_commands, project_commands
from athena.core import (
    db,
    deployment,
    run_context,
    token_commands,
    tokens,
    user_commands,
    users,
)
from athena.mcp.config import claude_mcp_config
from athena.mentor import page_commands, space_commands

DEMO_EMAIL = "operator@athena.local"
DEMO_PASSWORD = "athena-demo"
DEMO_RUN_ID = "demo-sol-run-001"


class DemoSetupError(Exception):
    """A safe, user-actionable refusal while creating a demo workspace."""


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _default_attach_dir(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.stem}-attachments")


def _remove_owned_demo_files(db_path: Path, attach_dir: Path) -> None:
    """Clean only paths this invocation created after a failed seed."""
    for candidate in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ):
        candidate.unlink(missing_ok=True)
    try:
        attach_dir.rmdir()
    except (FileNotFoundError, OSError):
        # A non-empty directory is never recursively removed. That makes cleanup
        # conservative if a future seed starts writing attachment fixtures.
        pass


def seed_demo(
    db_path: str | Path,
    *,
    attach_dir: str | Path | None = None,
    field_guide: bool = False,
) -> dict:
    """Create and seed a brand-new synthetic workspace.

    The database and attachment directory must not already exist. Returns paths,
    disposable login details, object ids, and counts for the CLI and tests.

    ``field_guide=True`` also seeds the agent Field Guide (``athena.guide``) into
    the same workspace, authored by the demo operator. It is the same seeding
    function ``athena-field-guide`` runs against an instance you keep — this
    tool's contract (a NEW database, never an existing one) is what makes the two
    entry points different, not the content.
    """
    db_path = Path(db_path).expanduser().resolve()
    attach_path = (
        Path(attach_dir).expanduser().resolve()
        if attach_dir is not None
        else _default_attach_dir(db_path)
    )
    if db_path == attach_path:
        raise DemoSetupError("database and attachment paths must be different")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    attach_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        raise DemoSetupError(f"database already exists: {db_path}")
    if attach_path.exists():
        raise DemoSetupError(f"attachment path already exists: {attach_path}")

    try:
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DemoSetupError(f"database already exists: {db_path}") from exc
    os.close(fd)

    conn = None
    try:
        attach_path.mkdir()
        conn = db.connect(db_path)
        db.migrate(conn)

        operator = user_commands.create_user(
            conn,
            actor_id=None,
            email=DEMO_EMAIL,
            name="Demo Operator",
            password=DEMO_PASSWORD,
            role=users.ADMIN_ROLE,
        )
        sol = user_commands.create_user(
            conn,
            actor_id=operator["id"],
            email="sol@athena.local",
            name="Sol Builder",
            role=users.MEMBER_ROLE,
            is_agent=True,
        )
        terra = user_commands.create_user(
            conn,
            actor_id=operator["id"],
            email="terra@athena.local",
            name="Terra Reviewer",
            role=users.MEMBER_ROLE,
            is_agent=True,
        )

        # Every seeded write goes through its command owner, so the demo database
        # — the review-facing tour of a load-bearing audit log — shows a complete
        # trail: each project, space, and page appears WITH its creation event.
        project = project_commands.create_project(
            conn,
            actor_id=operator["id"],
            name="Athena Review",
            key="ATH",
            description="Synthetic work for the five-minute reviewer tour.",
        )
        command_issue = issue_commands.create_issue(
            conn,
            actor=operator,
            project_id=project["id"],
            title="Finish command authorization boundary",
            body="Keep the adapter thin and the audited command authoritative.",
            priority="high",
        )
        demo_issue = issue_commands.create_issue(
            conn,
            actor=operator,
            project_id=project["id"],
            title="Capture the five-minute reviewer path",
            body="Walk through the seeded dashboard and [[issue:%d]]."
            % command_issue["id"],
            priority="medium",
        )
        docs_issue = issue_commands.create_issue(
            conn,
            actor=operator,
            project_id=project["id"],
            title="Publish contributor and security guidance",
            body="Make the project boundaries easy to evaluate.",
            priority="medium",
        )
        issue_commands.update_issue(
            conn,
            actor=operator,
            issue_id=command_issue["id"],
            assignee_id=sol["id"],
            status="in_progress",
        )
        issue_commands.update_issue(
            conn,
            actor=operator,
            issue_id=demo_issue["id"],
            assignee_id=terra["id"],
        )
        issue_commands.update_issue(
            conn,
            actor=operator,
            issue_id=docs_issue["id"],
            status="done",
        )

        run_token = run_context.set_run_id(DEMO_RUN_ID)
        try:
            issue_commands.update_issue(
                conn,
                actor=sol,
                issue_id=command_issue["id"],
                body=(
                    "Authorization now lives in the command. Review the linked "
                    "demo task at [[issue:%d]]." % demo_issue["id"]
                ),
            )
        finally:
            run_context.reset_run_id(run_token)

        space = space_commands.create_space(
            conn,
            actor_id=operator["id"],
            key="OPS",
            name="Operator Playbook",
            description="How the operator directs, observes, and intervenes.",
        )
        guide = page_commands.create_page(
            conn,
            actor_id=operator["id"],
            space_id=space["id"],
            title="Fleet operating guide",
            body=(
                # The seeded guide predated the trail chain (0072) and run
                # controls, so a reviewer following it saw neither of the two
                # things that most distinguish this tool. It names them now, and
                # names their limits in the same breath — a demo that oversells
                # is worse than one that is thin.
                "# Fleet operating guide\n\n"
                "Start with [[issue:%d]], inspect its activity, then replay run "
                "`%s`.\n\nThe operator remains accountable for every merge.\n\n"
                "## What to look at, and what it proves\n\n"
                "- **Admin -> Security** shows the activity trail's hash-chain "
                "head, re-verified on that render. It makes tampering evident, "
                "not impossible: a removed row is refused, an edited one is "
                "detected by verification. It cannot make an honest claim out of "
                "a dishonest one.\n"
                "- **A run's lineage page** can record a *run control* - steer, "
                "request cancel, or request a fresh-context handoff. Athena "
                "records the ask and the agent's reply. It cannot signal a "
                "process, so an unanswered control reads as expired, never as "
                "obeyed.\n"
                "- `athena-doctor <db>` walks the whole chain offline. Run it "
                "after any restore." % (command_issue["id"], DEMO_RUN_ID)
            ),
        )

        review_run_token = run_context.set_run_id("demo-terra-run-001")
        try:
            protocol = page_commands.create_page(
                conn,
                actor_id=terra["id"],
                space_id=space["id"],
                parent_id=guide["id"],
                title="Review protocol",
                body=(
                    "# Review protocol\n\n"
                    "1. Read the boundary docs.\n"
                    "2. Inspect [[issue:%d]].\n"
                    "3. Run the complete local gate." % demo_issue["id"]
                ),
            )
        finally:
            run_context.reset_run_id(review_run_token)

        # Mint Sol a least-privilege API token through the audited command, so the
        # demo's five-minute tour includes the differentiator: connecting a real AI
        # agent over MCP. The raw secret exists only in this return value/printout —
        # the database stores its hash, and the mint itself is on the activity trail.
        agent_token = token_commands.mint_token(
            conn,
            actor_id=sol["id"],
            name="demo-mcp",
            scopes=[
                tokens.READ_SCOPE,
                tokens.ISSUE_WRITE_SCOPE,
                tokens.DOCS_WRITE_SCOPE,
            ],
        )

        # Seeded through the same function athena-field-guide runs, authored by the
        # demo operator — so the guide's pages carry real provenance here too, and
        # the counts below include them.
        if field_guide:
            field_guide_content.seed_field_guide(conn, author_id=operator["id"])

        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("users", "projects", "issues", "spaces", "pages", "activity")
        }
        return {
            "db_path": str(db_path),
            "attach_dir": str(attach_path),
            "email": DEMO_EMAIL,
            "password": DEMO_PASSWORD,
            "run_id": DEMO_RUN_ID,
            "agent_email": "sol@athena.local",
            "agent_token": agent_token["token"],
            "agent_scopes": agent_token["scopes"],
            "ids": {
                "operator": operator["id"],
                "sol": sol["id"],
                "terra": terra["id"],
                "project": project["id"],
                "space": space["id"],
                "guide": guide["id"],
                "protocol": protocol["id"],
                "command_issue": command_issue["id"],
                "demo_issue": demo_issue["id"],
                "docs_issue": docs_issue["id"],
            },
            "counts": counts,
        }
    except BaseException:
        if conn is not None:
            conn.close()
            conn = None
        _remove_owned_demo_files(db_path, attach_path)
        raise
    finally:
        if conn is not None:
            conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="athena-demo",
        description="Create a synthetic Athena workspace and serve it on loopback.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="new SQLite database path; Athena refuses to overwrite an existing path",
    )
    parser.add_argument(
        "--attach-dir",
        type=Path,
        help="new attachment directory (default: <database-stem>-attachments)",
    )
    parser.add_argument("--port", type=_port, default=8000)
    parser.add_argument(
        "--field-guide",
        action="store_true",
        help=(
            "also seed the agent Field Guide as pages in the demo workspace "
            "(for an instance you keep, use athena-field-guide)"
        ),
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="create the workspace without starting the web server",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        seeded = seed_demo(
            args.db, attach_dir=args.attach_dir, field_guide=args.field_guide
        )
    except (DemoSetupError, field_guide_content.FieldGuideError) as exc:
        print(f"athena-demo: {exc}", file=sys.stderr)
        return 1

    print("Athena demo workspace created")
    print(f"  Database: {seeded['db_path']}")
    print(f"  Email:    {seeded['email']}")
    print(f"  Password: {seeded['password']}")
    print(f"  Run:      {seeded['run_id']}")
    print()
    print("Connect an AI agent over MCP (Sol's least-privilege token, shown once —")
    print("its mint is already on the activity trail):")
    print(
        json.dumps(
            claude_mcp_config(
                base_url=f"http://127.0.0.1:{args.port}",
                token=seeded["agent_token"],
            ),
            indent=2,
        )
    )
    if args.seed_only:
        return 0

    # A disposable reviewer server should not deliver webhooks or execute automation
    # in the background. It also has its own attachment root and no public bind flag.
    config.WEBHOOK_DELIVERY_ENABLED = False
    config.AUTOMATION_ENABLED = False
    config.ATTACH_DIR = Path(seeded["attach_dir"])
    config.TRUST_ACTOR_HEADER = False

    import uvicorn
    from athena.main import create_app

    url = f"http://127.0.0.1:{args.port}"
    print(f"  URL:      {url}")
    print(
        "Press Ctrl+C to stop; delete the database and attachment directory afterward."
    )
    uvicorn.run(
        create_app(
            Path(seeded["db_path"]),
            network_mode=deployment.LOCAL_MODE,
            allowed_authorities=deployment.local_authorities("127.0.0.1", args.port),
            expected_server=("127.0.0.1", args.port),
        ),
        host="127.0.0.1",
        port=args.port,
        workers=1,
        reload=False,
        lifespan="on",
        proxy_headers=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - console-script parity
    raise SystemExit(main())
