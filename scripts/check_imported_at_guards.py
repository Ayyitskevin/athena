"""Pin every native-only activity reader to its ``imported_at`` guard.

The doctrine (docs/FORGE.md, migration 0041): an imported row is foreign
history — something Athena was TOLD, not work it did. Every mechanism that
ACTS on the trail (projections, rollups, counters, automation scans, undo)
must exclude it, or a hostile import bundle or forge delivery moves native
state. H-0.3 (an imported forge event moved an issue's status through a
wildcard automation rule) and H-0.4 (the security counters and attention
rollup counted imported rows while the docs claimed otherwise) are what
happens when that rule is enforced by hand.

The check is greppable by design. Every SQL read of the ``activity`` table in
``src/athena`` must appear below — either as GUARDED (the reader's function
carries the guard, verified by pattern) or as EXEMPT (a general reader, with
the reason imported rows are safe there). A new reader that appears in neither
list fails the build; an entry that no longer matches a live reader fails as
stale. docs/FORGE.md's guard table is pinned to MECHANISMS by
tests/test_imported_at_guards.py, so the doc cannot claim a guard the code
does not carry — the exact H-0.4 failure.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"

# Case-sensitive on purpose: the codebase writes SQL keywords uppercase, and
# matching case keeps prose ("events from activity") out of the net. The word
# boundary keeps activity_target_id_sequences and activity_visibility_projects
# from matching.
_ACTIVITY_READ_RE = re.compile(r"\bFROM\s+activity\b|\bJOIN\s+activity\b")

# The native-only mechanisms, in the order docs/FORGE.md's guard table lists
# them. Each assertion is (path under src/athena, function or None for a
# whole-file check, pattern that must appear). The guard column is the
# substring FORGE.md's table cell must contain; the pin test compares labels
# exactly so the table cannot grow or rename a row without touching this list.
MECHANISMS: tuple[tuple[str, str, tuple[tuple[str, str | None, str], ...]], ...] = (
    (
        "Undo",
        "undo_imported_event",
        (("core/undo.py", "undo", r'event\["imported_at"\] is not None'),),
    ),
    (
        "Lifecycle facts (0055)",
        "imported_at IS NULL",
        (
            (
                "core/migrations/0055_issue_lifecycle_facts.sql",
                None,
                r"imported_at IS NULL",
            ),
        ),
    ),
    (
        "Claim handoffs (0058)",
        "imported_at IS NULL",
        (
            (
                "core/migrations/0058_issue_claim_handoffs.sql",
                None,
                r"imported_at IS NULL",
            ),
        ),
    ),
    (
        "Assignee facts (0068)",
        "imported_at IS NULL",
        (
            (
                "core/migrations/0068_issue_assignee_facts.sql",
                None,
                r"imported_at IS NULL",
            ),
        ),
    ),
    (
        "Fleet metrics",
        "imported_at IS NULL",
        (
            (
                "aegis/fleet_metrics.py",
                "_load_visible_lifecycle",
                r"imported_at IS NULL",
            ),
            # The evidence query deliberately SELECTs imported rows so coverage
            # can count them as excluded; the drop happens row-by-row here.
            (
                "aegis/fleet_metrics.py",
                "build_fleet_metrics",
                r'row\["imported_at"\] is not None',
            ),
        ),
    ),
    (
        "Attention rollup, security refusal counters",
        "imported_at IS NULL",
        (
            ("aegis/fleet_attention.py", "build_attention", r"imported_at IS NULL"),
            ("core/security_events.py", "list_failures", r"imported_at IS NULL"),
            ("core/security_events.py", "failure_counts", r"imported_at IS NULL"),
        ),
    ),
    (
        "Automation scan",
        "imported_at IS NULL",
        (
            # The scan opts into native-only at the call site (H-0.3); the
            # guard itself lives in list_events' native_only branch.
            ("aegis/automation.py", "process_pending", r"native_only=True"),
            ("core/activity.py", "list_events", r"imported_at IS NULL"),
        ),
    ),
)

# Guarded readers that are not named in FORGE.md's table: native-only
# projections that already carry the filter. (path, function, pattern, why).
EXTRA_GUARDED: tuple[tuple[str, str, str, str], ...] = (
    (
        "aegis/fleet_work.py",
        "build_active_work",
        r"imported_at IS NULL",
        "the live-issue claim join is a native-only projection of who holds "
        "the work; foreign history must not look like a live claim",
    ),
    (
        "aegis/automation.py",
        "_schedule_target_query",
        r"AND a\.imported_at IS NULL",
        "the inactive_for schedule condition measures NATIVE inactivity: a "
        "forge delivery or import bundle is foreign history and must not "
        "reset the clock on work it never touched (the schedule twin of the "
        "event scan's native_only guard)",
    ),
    (
        "aegis/room_briefs.py",
        "_decisions",
        r"a\.imported_at IS NULL",
        "the live decision projection must not promote foreign history into "
        "current room status",
    ),
    (
        "aegis/room_briefs.py",
        "_recent_timeline",
        r"a\.imported_at IS NULL",
        "the live brief timeline is an operational native-only projection",
    ),
    (
        "aegis/room_commands.py",
        "_authorize_reference",
        r"a\.imported_at IS NULL",
        "room writes may reference only native activity and run evidence",
    ),
    (
        "aegis/room_context.py",
        "_issue_scope",
        r"a\.imported_at IS NULL",
        "foreign agent activity must not widen a room's operational issue scope",
    ),
    (
        "aegis/room_context.py",
        "_latest_visible_activity_id",
        r"a\.imported_at IS NULL",
        "context revision receipts describe native Athena work only",
    ),
    (
        "aegis/room_timeline.py",
        "_candidate_agents",
        r"a\.imported_at IS NULL",
        "foreign authorship must not make an agent look like a live teammate",
    ),
    (
        "aegis/room_timeline.py",
        "_recent_contributions",
        r"a\.imported_at IS NULL",
        "agent contribution and lineage projections describe native work only",
    ),
    (
        "aegis/room_timeline.py",
        "_resolve_reference",
        r"a\.imported_at IS NULL",
        "activity and run references used as operational evidence must be native",
    ),
    (
        "aegis/rooms.py",
        "<module>",
        r"WHERE a\.imported_at IS NULL",
        "the room-authored event reader is a native-only projection",
    ),
    (
        "core/search.py",
        "_visibility_clause",
        r"room_activity\.imported_at IS NULL",
        "room-event search is an actionable projection and excludes foreign history",
    ),
)

# General readers allowed to see imported rows, each with the reason that is
# safe. "<module>" keys a module-level SQL constant. A site listed here that
# no longer reads the activity table fails as stale.
EXEMPT_READERS: tuple[tuple[str, str, str], ...] = (
    (
        "aegis/claim_handoffs.py",
        "<module>",
        "the projection joins activity by the handoff row's own yield/resume "
        "event ids; handoff rows are written only by the 0058 trigger, which "
        "already excludes imported events",
    ),
    (
        "aegis/automation.py",
        "_already_fired",
        "run-scoped existence check; imported rows carry NULL run coordinates "
        "(activity.record enforces), so foreign history can never satisfy "
        "run_id = ?",
    ),
    (
        "aegis/fleet_metrics.py",
        "_evidence_statement",
        "the evidence query deliberately SELECTs imported rows so coverage can "
        "report them as excluded_imported_events; build_fleet_metrics drops "
        "them row-by-row, asserted in MECHANISMS",
    ),
    (
        "aegis/fleet_work.py",
        "_replay_ready_run_ids",
        "run-scoped; imported rows carry NULL run coordinates",
    ),
    (
        "aegis/work_context.py",
        "_recent_activity",
        "the feed serves the trail as-is and SELECTs imported_at so surfaces "
        "can label foreign rows; a labeled feed is not a native-only mechanism",
    ),
    (
        "aegis/room_timeline.py",
        "_timeline_rows",
        "the public room timeline deliberately selects imported_at and labels "
        "foreign history; native briefs, context, agents, and contributions opt "
        "into its native_only branch",
    ),
    (
        "aegis/room_timeline.py",
        "_safe_native_complete_run_ids",
        "the provenance gate deliberately counts imported rows so any mixed "
        "native/foreign run loses its receipt; no imported row is projected as "
        "live coordination",
    ),
    (
        "core/activity.py",
        "<module>",
        "_SELECT backs the general audit readers (list_activity, the event "
        "stream); the native-only consumer applies the guard in list_events' "
        "native_only branch, asserted in MECHANISMS",
    ),
    (
        "core/activity.py",
        "_validated_lineage",
        "run/fork coordinates are validated by primary key for the native row "
        "being recorded; imported rows carry NULL run coordinates, and forking "
        "from a visible trail row is a native act with honest provenance",
    ),
    (
        "core/activity.py",
        "reversed_event_ids",
        "reversal links are written only by undo, which refuses imported "
        "events; this read projects the visible trail for undo badges",
    ),
    (
        "core/activity.py",
        "_child_run_ids",
        "run-scoped; imported rows carry NULL run coordinates",
    ),
    (
        "core/activity.py",
        "can_see_complete_run",
        "run-scoped visibility gate; imported rows carry NULL run coordinates",
    ),
    (
        "core/activity.py",
        "distinct_verbs",
        "filter metadata over the visible trail; the trail as-is includes "
        "foreign history",
    ),
    (
        "core/activity.py",
        "distinct_target_kinds",
        "filter metadata over the visible trail; the trail as-is includes "
        "foreign history",
    ),
    (
        "core/activity.py",
        "distinct_actors",
        "filter metadata over the visible trail; the trail as-is includes "
        "foreign history",
    ),
    (
        "core/agents.py",
        "agent_run_exists",
        "run-scoped; imported rows carry NULL run coordinates",
    ),
    (
        "core/agents.py",
        "_child_run_count",
        "run-scoped; imported rows carry NULL run coordinates",
    ),
    (
        "core/access.py",
        "can_see_complete_issue_history",
        "a visibility gate over the trail the actor may see, native or not; "
        "excluding foreign rows here would change what 'full history' means",
    ),
    (
        "core/webhooks.py",
        "current_tip",
        "the delivery cursor spans the whole firehose; webhook subscribers to "
        "the trail receive foreign history too, honestly marked",
    ),
    (
        "core/portability.py",
        "_activity_for_targets",
        "export must carry foreign history (with its imported_at marker) or "
        "the bundle would silently drop part of the trail",
    ),
    (
        "core/dispatch.py",
        "fork_run_ids",
        "run-scoped; imported rows carry NULL run coordinates",
    ),
    (
        "core/dispatch.py",
        "digest_mismatch_recorded",
        "run-scoped retry deduplication; imported rows carry NULL run "
        "coordinates and cannot satisfy run_id = ?",
    ),
    (
        "core/notifications.py",
        "process_mentions",
        "called from the native record wrappers with the id of the event just "
        "written; the import paths (forge, portability) never pass through "
        "them",
    ),
    (
        "core/notifications.py",
        "<module>",
        "the inbox join is keyed by the notification row's own event id; "
        "notification rows are created only by native fan-out, and the inbox "
        "renders the trail as-is",
    ),
    (
        "core/notifications.py",
        "unread_count",
        "visibility-gated count over the reader's own inbox; personal state",
    ),
    (
        "core/notifications.py",
        "mark_read",
        "visibility subquery for the reader's own inbox; personal state",
    ),
    (
        "core/notifications.py",
        "mark_all_read",
        "visibility subquery for the reader's own inbox; personal state",
    ),
)


class GuardCheckError(RuntimeError):
    """A guard or the reader inventory drifted from the code."""


@dataclass(frozen=True)
class Violation:
    """One unguarded, unlisted, or stale activity reader."""

    path: Path
    line: int
    detail: str


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _read_trees(package_root: Path) -> dict[str, tuple[Path, str, ast.AST]]:
    modules: dict[str, tuple[Path, str, ast.AST]] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise GuardCheckError(
                f"cannot parse {path}:{exc.lineno}: {exc.msg}"
            ) from exc
        modules[relative] = (path, source, tree)
    return modules


def _activity_read_sites(
    modules: dict[str, tuple[Path, str, ast.AST]],
) -> dict[tuple[str, str], list[int]]:
    """Every SQL read of the activity table, keyed by (file, function)."""
    sites: dict[tuple[str, str], list[int]] = {}
    for relative, (_path, _source, tree) in sorted(modules.items()):
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not _ACTIVITY_READ_RE.search(node.value):
                continue
            owner = "<module>"
            for function in functions:
                if function.lineno <= node.lineno <= (function.end_lineno or 0):
                    owner = function.name
            sites.setdefault((relative, owner), []).append(node.lineno)
    return sites


def find_violations(package_root: Path) -> list[Violation]:
    modules = _read_trees(package_root)
    violations: list[Violation] = []

    # 1. Every mechanism assertion must hold in the current source.
    for label, _guard, assertions in MECHANISMS:
        for relative, function, pattern in assertions:
            path = package_root / relative
            if relative.endswith(".sql"):
                if not path.is_file():
                    violations.append(
                        Violation(path, 1, f"{label}: missing migration {relative}")
                    )
                    continue
                if not re.search(pattern, path.read_text(encoding="utf-8")):
                    violations.append(
                        Violation(
                            path,
                            1,
                            f"{label}: {relative} no longer carries the guard "
                            f"/{pattern}/",
                        )
                    )
                continue
            entry = modules.get(relative)
            if entry is None:
                violations.append(
                    Violation(path, 1, f"{label}: missing module {relative}")
                )
                continue
            file_path, source, tree = entry
            target = _functions(tree).get(function) if function else None
            if function and target is None:
                violations.append(
                    Violation(
                        file_path,
                        1,
                        f"{label}: {relative} has no function {function} — "
                        f"a rename must move the guard assertion, not drop it",
                    )
                )
                continue
            segment = (
                ast.get_source_segment(source, target) if target is not None else source
            ) or ""
            if not re.search(pattern, segment):
                violations.append(
                    Violation(
                        file_path,
                        target.lineno if target is not None else 1,
                        f"{label}: {relative}::{function} lost its guard /{pattern}/",
                    )
                )

    # 2. Every SQL read of the activity table must be a known reader.
    sites = _activity_read_sites(modules)
    guarded_sites = {
        (relative, function or "<module>")
        for _label, _guard, assertions in MECHANISMS
        for relative, function, _pattern in assertions
        if relative.endswith(".py")
    } | {(relative, function) for relative, function, _p, _w in EXTRA_GUARDED}
    exempt_sites = {(relative, function) for relative, function, _why in EXEMPT_READERS}

    for (relative, function), lines in sorted(sites.items()):
        path = modules[relative][0]
        if (relative, function) in guarded_sites or (relative, function) in (
            exempt_sites
        ):
            continue
        violations.append(
            Violation(
                path,
                lines[0],
                f"new activity reader {relative}::{function} is in neither "
                f"the guarded nor the exempt list — a native-only reader must "
                f"filter imported_at IS NULL; a general reader must name why "
                f"foreign history is safe",
            )
        )

    # 3. Extra guarded readers must still carry their guard.
    for relative, function, pattern, why in EXTRA_GUARDED:
        entry = modules.get(relative)
        path = package_root / relative
        if entry is None:
            violations.append(Violation(path, 1, f"missing module {relative}"))
            continue
        file_path, source, tree = entry
        target = None if function == "<module>" else _functions(tree).get(function)
        if function != "<module>" and target is None:
            violations.append(
                Violation(file_path, 1, f"{relative} has no function {function}")
            )
            continue
        segment = (
            source if target is None else (ast.get_source_segment(source, target) or "")
        )
        if not re.search(pattern, segment):
            violations.append(
                Violation(
                    file_path,
                    target.lineno if target is not None else 1,
                    f"{relative}::{function} lost its guard /{pattern}/ ({why})",
                )
            )

    # 4. Listed readers that no longer read the trail are stale entries.
    for relative, function, why in sorted(EXEMPT_READERS):
        if (relative, function) not in sites:
            path = package_root / relative
            violations.append(
                Violation(
                    path,
                    1,
                    f"stale exemption: {relative}::{function} no longer reads "
                    f"the activity table — delete the entry ({why})",
                )
            )
    return violations


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check that every native-only activity reader excludes "
        "imported rows"
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=PACKAGE_ROOT,
        help="Athena package root (default: repository src/athena)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    package_root = args.package_root.absolute()

    try:
        violations = find_violations(package_root)
    except (GuardCheckError, OSError, UnicodeError) as exc:
        print(f"Athena imported_at guards failed: {exc}", file=sys.stderr)
        return 1

    if violations:
        count = len(violations)
        print(
            f"Athena imported_at guards failed: {count} "
            f"{'violation' if count == 1 else 'violations'}",
            file=sys.stderr,
        )
        for violation in violations:
            print(
                f"{violation.path}:{violation.line}: {violation.detail}",
                file=sys.stderr,
            )
        return 1

    print(
        "Athena imported_at guards passed: every native-only activity reader "
        "excludes foreign history"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
