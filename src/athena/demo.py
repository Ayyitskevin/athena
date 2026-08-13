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
from athena.aegis import (
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    project_commands,
)
from athena.core import (
    agent_run_commands,
    approvals,
    budgets,
    db,
    deployment,
    run_context,
    run_control_commands,
    run_controls,
    token_commands,
    tokens,
    user_commands,
    users,
    worker_commands,
)
from athena.mcp.config import claude_mcp_config
from athena.mentor import page_commands, space_commands

DEMO_EMAIL = "operator@athena.local"
DEMO_PASSWORD = "athena-demo"
DEMO_RUN_ID = "demo-sol-run-001"
DEMO_WORKER_KEY = "demo-sol-worker-1"
DEMO_WORKER_NODE = "demo-laptop"

# Sol's ceiling for the tour. Low enough that the cockpit shows a real fraction
# consumed rather than a rounding error against a number nobody would ever hit —
# the point of the budget surface is that it reads as a live constraint.
DEMO_BUDGET_WINDOW = "hour"
DEMO_BUDGET_ACTION_LIMIT = 25


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


def _seed_supervision(
    conn,
    *,
    operator: dict,
    sol: dict,
    raw_agent_token: str,
    work_issue_id: int,
) -> dict:
    """Seed the supervision state the Intervene and Trust surfaces exist to show.

    Without this the demo seeds a tracker and a wiki, every supervision surface
    renders its empty state, and the five-minute tour makes Athena look like a
    small Notion — the differentiator (agents are first-class actors you can
    watch, bound, and interrupt) is invisible precisely where it should be loudest.

    Everything below is seeded through the real commands, as SOL, holding Sol's own
    bearer token — resolved through `tokens.resolve_token`, the same function the
    HTTP layer authenticates with, so the actor dict here is the one a real agent
    request carries rather than a hand-built lookalike. A worker registration or a
    run check-in fabricated around the credential checks would be exactly the
    fiction those checks exist to keep out of the registry.

    demo.py's standing rule holds: **a demo that oversells is worse than one that
    is thin.** Every state below is TRUE in the workspace it describes. The pending
    approval is genuinely pending, because Sol genuinely tried to close an issue it
    is genuinely gated on and was genuinely refused. The open run control is
    genuinely unanswered. Nothing here is a screenshot.
    """
    # The agent's own credential, resolved the way the server resolves it.
    sol_agent = tokens.resolve_token(conn, raw_agent_token)
    if sol_agent is None:
        raise DemoSetupError("could not resolve the freshly minted agent token")

    # 0. The ceiling comes FIRST, because a budget only meters what happens after it
    #    exists. Setting it last would leave the cockpit showing a limit with zero
    #    consumption — a control that has never been felt, which teaches the reviewer
    #    nothing about what it does. The operator bounds the agent, and then the agent
    #    works against the bound.
    budget = budgets.set_budget(
        conn,
        actor_id=operator["id"],
        target_user_id=sol["id"],
        window=DEMO_BUDGET_WINDOW,
        action_limit=DEMO_BUDGET_ACTION_LIMIT,
    )

    # 1. A live claim, with the check-in that makes it legible as live. This is the
    #    lease Mission Control shows: who holds the work, and when the hold lapses.
    #    The If-Match is not ceremony — claiming is a compare-and-swap, and the demo
    #    takes the same lock a real agent takes.
    claim_target = issues.get_issue(conn, work_issue_id)
    if claim_target is None:
        raise DemoSetupError("the issue to claim disappeared during seeding")
    lease = lease_commands.claim_issue(
        conn,
        actor=sol_agent,
        issue_id=work_issue_id,
        if_match=[issue_etags.current_etag(conn, claim_target)],
    )
    run_token = run_context.set_run_id(DEMO_RUN_ID)
    try:
        checkin = agent_run_commands.heartbeat(
            conn, actor=sol_agent, run_id=DEMO_RUN_ID
        )
    finally:
        run_context.reset_run_id(run_token)

    # 2. A worker in the registry, heartbeating. The registry answers "what
    #    processes are out there acting as this agent", which is a different
    #    question from "what is this run doing" — the tour should show both.
    worker = worker_commands.heartbeat(
        conn,
        actor=sol_agent,
        worker_key=DEMO_WORKER_KEY,
        node_label=DEMO_WORKER_NODE,
        capabilities=["issues", "docs"],
    )

    # 3. Sol does the work it claimed, and says so. A metered write, so the budget
    #    above now reads as a live constraint with real consumption against it —
    #    and the trail carries the agent's account of what it did, which is the
    #    thing the operator is being asked to approve in the next step.
    progress_token = run_context.set_run_id(DEMO_RUN_ID)
    try:
        issue_commands.update_issue(
            conn,
            actor=sol_agent,
            issue_id=work_issue_id,
            body=(
                "Authorization now lives in the command, and the adapter only maps "
                "the refusal kind onto a status code. Ready to close — asking "
                "first, because the operator gates this one."
            ),
        )
    finally:
        run_context.reset_run_id(progress_token)

    # 4. A pending approval — earned, not inserted. The operator gates Sol on
    #    issue.close; Sol then actually attempts the close and is actually refused,
    #    and the refusal's ask is recorded exactly the way main.py's
    #    ApprovalRequired handler records it: on the freed connection, AFTER the
    #    refused command's transaction unwound, because a row written inside that
    #    transaction would have rolled back with the refusal.
    approvals.set_policy(
        conn,
        actor_id=operator["id"],
        target_user_id=sol["id"],
        action_kind=approvals.ACTION_ISSUE_CLOSE,
    )
    approval = None
    ask_token = run_context.set_run_id(DEMO_RUN_ID)
    try:
        issue_commands.update_issue(
            conn, actor=sol_agent, issue_id=work_issue_id, status="done"
        )
    except approvals.ApprovalRequired as exc:
        approval = approvals.open_request(
            conn,
            actor_id=exc.actor_id,
            action_kind=exc.action_kind,
            target_kind=exc.target_kind,
            target_id=exc.target_id,
            run_id=run_context.get_run_id(),
        )
    finally:
        run_context.reset_run_id(ask_token)
    if approval is None:
        raise DemoSetupError("seeded approval gate did not refuse the demo close")

    # 5. An open run control: the operator has asked Sol's live run to change
    #    course, and Sol has not answered. Athena records the ask and the reply; it
    #    cannot signal a process, so this row is the honest shape of intervention —
    #    a request outstanding, not an instruction obeyed.
    control = run_control_commands.create_control(
        conn,
        actor=operator,
        run_id=DEMO_RUN_ID,
        kind=run_controls.KIND_STEER,
        payload=(
            "Narrow the scope: land the authorization boundary first and leave "
            "the docs pass for a follow-up."
        ),
    )

    return {
        "lease_expires_at": lease["expires_at"],
        "checkin_run_id": checkin["run_id"],
        "worker_id": worker["id"],
        "budget_window": budget.window,
        "budget_action_limit": budget.action_limit,
        "approval_id": approval.id,
        "approval_action": approval.action_kind,
        "run_control_id": control["id"],
        "run_control_kind": control["kind"],
    }


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
            actor=operator,
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
                actor=terra,
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

        # The supervision state: a live claim and check-in, a worker, a budget, a
        # pending approval, and an unanswered run control. Seeded AFTER the token
        # exists because every one of them is written as Sol, holding Sol's
        # credential — which is what makes them real rather than decorative.
        supervision = _seed_supervision(
            conn,
            operator=operator,
            sol=sol,
            raw_agent_token=agent_token["token"],
            work_issue_id=command_issue["id"],
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
            "supervision": supervision,
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
    # The tour's point is the supervision loop, so say what is waiting rather than
    # leaving the reviewer to find it. Both of these are genuinely unanswered.
    supervision = seeded["supervision"]
    print("Two things are waiting on you (this is the tour):")
    print(
        f"  - Approvals waiting on you: Sol asked to {supervision['approval_action']} "
        "the issue it just finished  ->  /admin/agents"
    )
    print(
        f"  - Run controls awaiting an agent: your {supervision['run_control_kind']} "
        "against Sol's live run is unanswered  ->  /admin/run-controls"
    )
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
