#!/usr/bin/env python3
"""Gather the usage evidence a prune review needs, from a real Athena database.

`docs/PRUNE_LEDGER.md` asks one question per subsystem — when was this last really
used? — and that question cannot be answered from the repository. Source code tells
you a feature EXISTS; only a database that someone has been working in tells you
whether anyone reached for it. So the ledger's evidence column is produced here,
against the dogfood deployment, rather than written by hand from memory.

    python scripts/prune_evidence.py /path/to/athena.db
    python scripts/prune_evidence.py /path/to/athena.db --markdown   # ledger table

Read-only: the database is opened in immutable mode, so pointing this at a live
deployment cannot disturb it.

**What this measures, and what it cannot.** Two kinds of evidence are collected:
row counts for the tables a subsystem owns, and the most recent activity event
carrying one of its verbs. Both are positive evidence — they show use. Neither can
prove disuse, and the difference matters:

  * A subsystem with **no verbs and no tables of its own** (a pure read surface: a
    graph view, a search page, an export) leaves no trace in the database at all.
    Zero here means "unmeasurable by this script", NOT "unused". Those rows are
    marked `n/a` rather than `0`, because a reviewer skimming a column of zeroes
    will not stop to make that distinction, and a wrong cut is much more expensive
    than a wrong keep.
  * A count of zero on a subsystem that DOES write means nobody has used it in this
    database. That is real evidence, and it is still not proof: a quarterly feature
    used once a year looks identical to a dead one over a single quarter.

Which is the point of a standing ledger rather than a one-time audit. Run it each
quarter, and it is the TREND that carries the argument — three consecutive quarters
of nothing is a case; one is a data point.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Subsystem:
    """One reviewable unit: what it is, and where its footprints are.

    Grouped by what a reviewer would decide about as a whole, not by module
    boundaries — parking "the answerability ledger" means parking its table, its
    routes and its web surface together, and a ledger that split those into three
    rows would be asking three questions that only have one answer.
    """

    name: str
    summary: str
    #: Tables this subsystem owns. Row counts are positive evidence of use.
    tables: tuple[str, ...] = ()
    #: Activity verbs it writes. The most recent one dates its last real use.
    verbs: tuple[str, ...] = ()
    #: Route prefixes, for the surface column. Not evidence — cost.
    routes: tuple[str, ...] = ()
    #: True when the subsystem writes nothing: a pure read surface, invisible to
    #: this script by construction. Reported as `n/a`, never as zero.
    read_only: bool = False
    #: Count events whose `imported_at` is set. Off by default, because imported
    #: rows are usually ANOTHER deployment's history and crediting them here would
    #: report work this deployment never did. Forge inbound is the exception that
    #: forced the flag to exist: its events land as imported history BY DESIGN
    #: ("Athena's record of what it was told"), so the default filter reported it
    #: as never used — an argument to cut a working subsystem, from a bug.
    counts_imported: bool = False
    notes: str = ""
    extras: dict[str, str] = field(default_factory=dict)


#: The subsystem map. Deliberately hand-written rather than derived: deciding what
#: counts as one reviewable thing IS the judgement a ledger exists to support, and
#: a generated grouping would quietly re-answer it every time the code moved.
SUBSYSTEMS: tuple[Subsystem, ...] = (
    Subsystem(
        "issues (Aegis core)",
        "Issues, comments, labels, links, projects, sprints — the product.",
        tables=(
            "issues",
            "comments",
            "issue_labels",
            "issue_links",
            "projects",
            "sprints",
        ),
        verbs=(
            "created",
            "commented",
            "changed_status",
            "assigned",
            "unassigned",
            "labeled",
            "unlabeled",
            "linked",
            "unlinked",
            "issue_edited",
            "changed_priority",
            "archived",
            "reopened",
            "moved_to_sprint",
        ),
        routes=("/issues", "/aegis", "/projects", "/sprints", "/labels"),
    ),
    Subsystem(
        "pages (Mentor core)",
        "Pages, versions, spaces, page comments — the knowledge half.",
        tables=("pages", "page_versions", "spaces", "page_comments", "page_labels"),
        verbs=(
            "page_created",
            "page_edited",
            "page_commented",
            "page_archived",
            "page_moved",
            "page_learning_recorded",
            "space_created",
        ),
        routes=("/pages", "/mentor", "/spaces"),
    ),
    Subsystem(
        "agent supervision",
        "Leases, claims, check-ins, workers, budgets, approvals, run controls — "
        "the fleet loop, and the differentiator VISION names.",
        tables=(
            "issue_leases",
            "issue_claim_handoffs",
            "agent_run_checkins",
            "agent_workers",
            "agent_budgets",
            "approval_requests",
            "run_controls",
            "run_bindings",
        ),
        # Verified against the codebase's verb vocabulary rather than guessed. The
        # first draft of this list invented `claim_started`-style names and reported
        # ZERO events against a demo database that plainly exercises supervision —
        # which is why test_prune_evidence now fails on any verb the code never
        # writes.
        verbs=(
            "claimed",
            "claim_completed",
            "claim_yielded",
            "lease_renewed",
            "claim_handoff_resumed",
            "worker_registered",
            "worker_stopped",
            "worker_kill_requested",
            "approval_requested",
            "approval_approved",
            "approval_rejected",
            "approval_policy_set",
            "agent_budget_set",
            "agent_budget_exhausted",
            "run_control_requested",
            "run_control_acknowledged",
            "run_control_completed",
        ),
        routes=("/workers", "/approvals", "/run-controls", "/agent-runs", "/fleet"),
    ),
    Subsystem(
        "activity trail",
        "The append-only event log and its chain — the thing the campaign called "
        "the product. Everything else writes through it.",
        tables=("activity", "activity_chain"),
        verbs=(),
        routes=("/activity", "/events"),
        notes="Measured by total row count; every other subsystem's verbs live here.",
    ),
    Subsystem(
        "automation rules",
        "Rule engine plus its scheduler: firings, occurrences, state.",
        # `automation_state` and `automation_schedule_state` are deliberately NOT
        # here: the migration seeds each with a singleton row, so they read 1 on a
        # database nobody has ever touched. A table that can never read zero is not
        # evidence of anything, and including it would quietly add a floor to this
        # subsystem's count. test_prune_evidence pins the rule by asserting every
        # measurable subsystem reads zero on a freshly migrated database.
        tables=(
            "automation_rules",
            "automation_schedule_firings",
            "automation_schedule_occurrences",
        ),
        verbs=(
            "created_automation_rule",
            "enabled_automation_rule",
            "disabled_automation_rule",
            "deleted_automation_rule",
            "scheduled",
        ),
        notes="These verbs record rule MANAGEMENT and scheduling. A rule firing "
        "writes the action's own verb (a status change looks like a status "
        "change), which is right for the trail and means firings cannot be "
        "counted here — check automation_schedule_firings for that.",
        routes=("/automation",),
    ),
    Subsystem(
        "webhooks (outbound)",
        "Outbound delivery to a receiver the operator registers.",
        tables=("webhooks",),
        verbs=(
            "registered_webhook",
            "paused_webhook",
            "resumed_webhook",
            "deleted_webhook",
        ),
        routes=("/webhooks",),
        notes="Verbs cover registration, not delivery; a webhook that fires "
        "hourly and one that has never fired look identical here.",
    ),
    Subsystem(
        "forge inbound",
        "Signed inbound delivery from a code forge, and the event sources that "
        "authorize it. NAMED IN F-3.4 AS A CANDIDATE TO EVALUATE.",
        tables=("event_sources",),
        verbs=(
            "forge_commit",
            "forge_pull_request",
            "forge_branch",
            "registered_event_source",
            "paused_event_source",
            "resumed_event_source",
            "deleted_event_source",
        ),
        routes=("/forge", "/event-sources"),
        counts_imported=True,
    ),
    Subsystem(
        "Icarus dispatch",
        "Outbound dispatch to an external executor, and its signed callbacks.",
        tables=("icarus_dispatches",),
        verbs=(
            "dispatch_requested",
            "dispatch_accepted",
            "dispatch_completed",
            "dispatch_evidence_recorded",
            "dispatch_undeliverable",
            "dispatch_policy_digest_mismatch",
        ),
        routes=("/dispatches", "/callbacks"),
    ),
    Subsystem(
        "knowledge graph / ego view",
        "The link graph and its per-node view. NAMED IN F-3.4 AS A CANDIDATE TO "
        "EVALUATE.",
        tables=("links",),
        verbs=(),
        routes=("/aegis/graph",),
        read_only=True,
        notes="Reads the `links` table that sync_links maintains for backlinks; the "
        "VIEW writes nothing, so its own use is not recorded anywhere. Row count "
        "below measures the link index, which backlinks also use — it is NOT "
        "evidence that anyone opened the graph.",
    ),
    Subsystem(
        "answerability ledger",
        "The can-this-be-answered ledger and its web surface. NAMED IN F-3.4 AS A "
        "CANDIDATE TO EVALUATE.",
        tables=(),
        verbs=(),
        routes=("/admin/answerability",),
        read_only=True,
        notes="Computed from existing data on read; owns no table and writes no "
        "verb, so this script cannot see it at all. Its use has to be judged from "
        "whether anyone opens the page.",
    ),
    Subsystem(
        "workspace search",
        "Full-text search across issues, pages and comments (FTS5).",
        tables=("search_index",),
        verbs=(),
        routes=("/search", "/find"),
        read_only=True,
        notes="The index is maintained on every write, so its size tracks the "
        "workspace, not search usage.",
    ),
    Subsystem(
        "playbooks / workflows",
        "Playbook definitions and nested checklists.",
        tables=(),
        verbs=(),
        routes=("/workflows",),
        read_only=True,
        notes="Writes no activity verb of its own — playbook progress is stored "
        "on the page, so this script cannot see it. Judge by whether playbook "
        "pages are being advanced.",
    ),
    Subsystem(
        "desk / cursors",
        "The desk orientation read and per-agent cursors.",
        tables=("agent_cursors",),
        verbs=(),
        routes=("/desk",),
    ),
    Subsystem(
        "delegation inbox",
        "Delegating an issue to another user and the receiving inbox.",
        tables=(),
        verbs=("delegated", "delegation_declined"),
        routes=("/delegations", "/inbox"),
    ),
    Subsystem(
        "portability (export/import)",
        "Bundle export and manifest-driven import.",
        tables=(),
        verbs=(),
        routes=(),
        read_only=True,
        notes="An import stamps `imported_at` on the rows it lands rather than "
        "writing a verb of its own, and export writes nothing at all. Count rows "
        "WHERE imported_at IS NOT NULL to see whether anything was ever imported; "
        "exports are invisible.",
    ),
    Subsystem(
        "recovery (backup/restore)",
        "Backup bundles, restore drills, the Linux-only publication path.",
        tables=(),
        verbs=(),
        routes=(),
        read_only=True,
        notes="CLI-only (athena-backup / athena-doctor). Invisible to this script; "
        "judge it by whether drills are being run, not by database contents.",
    ),
    Subsystem(
        "attachments",
        "Uploaded blobs and their target-gated download route.",
        tables=("attachments",),
        verbs=("added_attachment", "removed_attachment"),
        routes=("/attachments",),
    ),
    Subsystem(
        "notifications / watches",
        "Watching a target and the per-user inbox it feeds.",
        tables=("notifications", "watches"),
        verbs=(),
        routes=("/notifications", "/watches"),
    ),
    Subsystem(
        "saved filters",
        "Stored work-query criteria.",
        tables=("saved_filters",),
        verbs=(),
        routes=("/filters",),
    ),
    Subsystem(
        "OIDC login",
        "SSO login, off unless all four connection settings are set.",
        tables=("oidc_identities", "oidc_login_states"),
        verbs=(),
        routes=("/auth",),
    ),
)


def _connect(path: Path) -> sqlite3.Connection:
    """Open read-only. `immutable=1` also promises we will not even take a lock, so
    this is safe to point at a running deployment."""
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }


def _rows(conn: sqlite3.Connection, table: str) -> int:
    # Table names come from the frozen map above, never from input.
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _verb_evidence(
    conn: sqlite3.Connection, subsystem: Subsystem
) -> tuple[int, str | None]:
    """How many events carry this subsystem's verbs, and when the last one landed.

    `imported_at IS NULL` normally excludes foreign history: an imported bundle
    carries another deployment's events, and counting those would credit this one
    with work it never did. `counts_imported` lifts that for subsystems whose
    events are imported by design — see the field's own note.
    """
    verbs = subsystem.verbs
    if not verbs:
        return 0, None
    placeholders = ",".join("?" * len(verbs))
    imported_clause = "" if subsystem.counts_imported else " AND imported_at IS NULL"
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MAX(created_at) AS last FROM activity "
        f"WHERE verb IN ({placeholders}){imported_clause}",
        verbs,
    ).fetchone()
    return int(row["n"]), row["last"]


@dataclass
class Finding:
    subsystem: Subsystem
    table_rows: dict[str, int]
    missing_tables: tuple[str, ...]
    verb_events: int
    last_seen: str | None

    @property
    def total_rows(self) -> int:
        return sum(self.table_rows.values())

    @property
    def dated(self) -> str:
        """What to print in the "last seen" column.

        Only a subsystem that writes VERBS can be dated — a row count has no
        timestamp. Printing "never" for a subsystem that simply has no verbs would
        read as "nobody has ever used this", which for the activity trail (46 rows
        and counting) is precisely backwards.
        """
        if self.last_seen:
            return self.last_seen
        if not self.subsystem.verbs:
            return "— (no dated verbs)"
        return "never"

    @property
    def measurable(self) -> bool:
        return not self.subsystem.read_only and bool(
            self.subsystem.tables or self.subsystem.verbs
        )

    @property
    def evidence(self) -> str:
        if not self.measurable:
            return "n/a — leaves no trace"
        parts = []
        if self.subsystem.tables:
            parts.append(f"{self.total_rows} rows")
        if self.subsystem.verbs:
            parts.append(f"{self.verb_events} events")
        return ", ".join(parts) or "n/a"


def collect(conn: sqlite3.Connection) -> list[Finding]:
    present = _existing_tables(conn)
    findings = []
    for subsystem in SUBSYSTEMS:
        counts, missing = {}, []
        for table in subsystem.tables:
            if table in present:
                counts[table] = _rows(conn, table)
            else:
                missing.append(table)
        events, last = _verb_evidence(conn, subsystem)
        findings.append(Finding(subsystem, counts, tuple(missing), events, last))
    return findings


def _print_plain(findings: list[Finding], path: Path) -> None:
    print(f"Prune evidence from {path}\n")
    width = max(len(f.subsystem.name) for f in findings)
    for finding in findings:
        print(
            f"  {finding.subsystem.name:<{width}}  {finding.evidence:<22} "
            f"last: {finding.dated}"
        )
        if finding.missing_tables:
            print(
                f"  {'':<{width}}  (tables absent from this database: "
                f"{', '.join(finding.missing_tables)})"
            )
    print(
        "\nZero is not proof of disuse, and `n/a` is not zero — read the module "
        "docstring before cutting anything on the strength of this table."
    )


def _print_markdown(findings: list[Finding], path: Path) -> None:
    print(f"<!-- generated by scripts/prune_evidence.py from {path.name} -->")
    print("| subsystem | surface | evidence of use | last seen | verdict |")
    print("|---|---|---|---|---|")
    for finding in findings:
        sub = finding.subsystem
        surface = ", ".join(f"`{r}`" for r in sub.routes) or "—"
        last = "**never**" if finding.dated == "never" else finding.dated
        print(
            f"| **{sub.name}** | {surface} | {finding.evidence} | {last} | _(unset)_ |"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prune_evidence.py",
        description="Collect usage evidence for docs/PRUNE_LEDGER.md (read-only).",
    )
    parser.add_argument("database", type=Path, help="path to an Athena SQLite database")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="emit the ledger's evidence table instead of a plain report",
    )
    args = parser.parse_args(argv)

    if not args.database.is_file():
        print(f"prune_evidence: no such database: {args.database}", file=sys.stderr)
        return 1
    conn = _connect(args.database)
    try:
        findings = collect(conn)
    finally:
        conn.close()

    if args.markdown:
        _print_markdown(findings, args.database)
    else:
        _print_plain(findings, args.database)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
