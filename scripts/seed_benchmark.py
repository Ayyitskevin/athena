#!/usr/bin/env python3
"""Seed a benchmark database with a realistic trail, for measuring read performance.

One seeder, two shapes.

``--shape trail`` (the default) is the shape the activity-feed work in
docs/plans/OPUS_PERFORMANCE_ADOPTION_GUIDE_ATHENA.md is stated at (10k issues /
100k events): issues created directly and a synthetic trail recorded through
``activity.record``, so the feed's visibility gate has history to read.

``--shape fleet`` is the shape docs/PERFORMANCE.md measures the hot paths at
(H8): a workspace a fleet of agents has worked in for months, written through
the real application commands, so every projection, notification, lease row
and event is the one the product writes. ``--issues`` 1000, 10000 and 50000 are
the three sizes the medians are stated at. It also writes ``PATH.json`` beside
the database: the bearer tokens of the accounts it made (for this scratch
database only) and the ids the benchmark asks about.

    python scripts/seed_benchmark.py /tmp/bench.db
    python scripts/seed_benchmark.py /tmp/small.db --issues 500 --events 5000
    python scripts/seed_benchmark.py /tmp/fleet10k.db --shape fleet --issues 10000

A before/after number in a PR is reproducible by anyone rather than a claim
about a database nobody else has.

WHY IT DOES NOT RUN ANALYZE (read this before "fixing" that)
------------------------------------------------------------
Nothing in the Athena product ever runs ANALYZE or PRAGMA optimize — grep the tree.
Every real Athena database therefore has NO sqlite_stat1 table, and SQLite plans
queries from its built-in heuristics rather than from statistics.

That distinction is not academic: for the gated activity feed it is the difference
between a 0.4 ms read and a 295 ms one. With statistics present SQLite walks the rowid
index backwards and stops at LIMIT; without them it resolves the visibility OR by
MULTI-INDEX OR and sorts every survivor through a temp B-tree before LIMIT applies. An
earlier version of this script ran ANALYZE as a "make the benchmark fair" gesture and
produced numbers ~800x better than production, which nearly retired a real ceiling as
a non-issue.

So: seed, then measure, in the state the product actually ships. If you want to explore
what statistics would buy, pass --analyze deliberately and label the number as such.

The trail is written through the real recorders (activity.record, create_issue,
create_page), so the hash chain, the visibility envelope and the per-event project
scope rows are all present and honest — a synthetic INSERT-only trail would measure a
schema Athena never actually has.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

if __package__ is None:  # allow running from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from athena.aegis import comment_commands, issue_commands  # noqa: E402
from athena.aegis import issue_etags, lease_commands  # noqa: E402
from athena.aegis import issues as issues_mod  # noqa: E402
from athena.aegis import projects as projects_mod  # noqa: E402
from athena.core import activity, db, search, tokens, users  # noqa: E402
from athena.mentor import pages as pages_mod  # noqa: E402
from athena.mentor import spaces as spaces_mod  # noqa: E402

# Mixed public/private on purpose: a benchmark where everything is public never
# exercises the membership half of the visibility predicate.
PUBLIC_PROJECTS = 8
PRIVATE_PROJECTS = 4
PUBLIC_SPACES = 3
PRIVATE_SPACES = 2
PAGES = 500
COMMIT_EVERY = 2_000

# Roughly the mix a working tracker accumulates: most events land on issues, a
# steady minority on wiki pages, a few on the containers themselves.
KIND_MIX = (("issue", 7), ("page", 2), ("project", 1))


def _progress(label: str, done: int, total: int) -> None:
    if done % (COMMIT_EVERY * 5) == 0 or done == total:
        print(f"  {label} {done}/{total}", file=sys.stderr, flush=True)


def seed(
    path: Path, *, issue_count: int, event_count: int, analyze: bool = False
) -> dict:
    if path.exists():
        path.unlink()
    conn = db.connect(path)
    db.migrate(conn)

    admin = users.create_user(
        conn, email="admin@example.test", name="Admin", role="admin", commit=True
    )
    owner = users.create_user(
        conn, email="owner@example.test", name="Owner", commit=True
    )
    member = users.create_user(
        conn, email="member@example.test", name="Member", commit=True
    )
    agent = users.create_user(
        conn, email="agent@example.test", name="Agent", is_agent=True, commit=True
    )

    projects: list[dict] = []
    for i in range(PUBLIC_PROJECTS + PRIVATE_PROJECTS):
        private = i >= PUBLIC_PROJECTS
        project = projects_mod.create_project(
            conn,
            name=f"{'Private' if private else 'Public'} {i}",
            key=f"{'PRV' if private else 'PUB'}{i}",
            created_by=owner["id"],
            commit=True,
        )
        if private:
            conn.execute(
                "UPDATE projects SET visibility = 'private' WHERE id = ?",
                (project["id"],),
            )
        projects.append(project)

    spaces: list[dict] = []
    for i in range(PUBLIC_SPACES + PRIVATE_SPACES):
        private = i >= PUBLIC_SPACES
        space = spaces_mod.create_space(
            conn,
            key=f"{'RS' if private else 'PS'}{i}",
            name=f"Space {i}",
            created_by=owner["id"],
            commit=True,
        )
        if private:
            conn.execute(
                "UPDATE spaces SET visibility = 'private' WHERE id = ?", (space["id"],)
            )
        spaces.append(space)
    conn.commit()

    started = time.perf_counter()
    issue_ids: list[int] = []
    conn.execute("BEGIN IMMEDIATE")
    for i in range(issue_count):
        bucket = i % (len(projects) + 1)  # the extra bucket is the backlog
        row = issues_mod.create_issue(
            conn,
            title=f"Issue {i} about thing {i % 97}",
            body=f"body {i}",
            created_by=owner["id"],
            status=("open", "in_progress", "done")[i % 3],
            priority=issues_mod.PRIORITIES[i % len(issues_mod.PRIORITIES)],
            project_id=None if bucket == len(projects) else projects[bucket]["id"],
            commit=False,
        )
        issue_ids.append(row["id"])
        if i % COMMIT_EVERY == COMMIT_EVERY - 1:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            _progress("issues", i + 1, issue_count)
    conn.commit()
    print(f"issues ({issue_count}) in {time.perf_counter() - started:.1f}s")

    page_ids: list[int] = []
    conn.execute("BEGIN IMMEDIATE")
    for i in range(PAGES):
        row = pages_mod.create_page(
            conn,
            space_id=spaces[i % len(spaces)]["id"],
            title=f"Page {i}",
            created_by=owner["id"],
            body=f"page body {i}",
            commit=False,
        )
        page_ids.append(row["id"])
    conn.commit()

    kinds: list[str] = []
    for kind, weight in KIND_MIX:
        kinds.extend([kind] * weight)
    verbs = ("issue_created", "issue_updated", "issue_closed", "comment_added")
    actors = [owner["id"], member["id"], admin["id"], agent["id"]]

    started = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    for i in range(event_count):
        kind = kinds[i % len(kinds)]
        if kind == "issue":
            target_id = issue_ids[i % len(issue_ids)]
        elif kind == "page":
            target_id = page_ids[i % len(page_ids)]
        else:
            target_id = projects[i % len(projects)]["id"]
        activity.record(
            conn,
            actor_id=actors[i % len(actors)],
            verb=verbs[i % len(verbs)],
            target_kind=kind,
            target_id=target_id,
            detail=f"detail {i}",
            commit=False,
        )
        if i % COMMIT_EVERY == COMMIT_EVERY - 1:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            _progress("events", i + 1, event_count)
    conn.commit()
    print(f"events ({event_count}) in {time.perf_counter() - started:.1f}s")

    if analyze:
        # Deliberate, and never the default — see the module docstring.
        conn.execute("ANALYZE")
        conn.commit()
        print("ANALYZE run: this database is NOT production-shaped")

    return {"admin": admin, "owner": owner, "member": member, "agent": agent}


# --- the fleet shape ---------------------------------------------------------

#: Accounts and containers of the fleet shape. Seat 0 is the account the
#: benchmark reads the desk as.
FLEET_SEATS = 6
FLEET_PUBLIC_PROJECTS = 5
FLEET_HOT_COMMENTS = 300
FLEET_TEXT = (
    "The worker ran the suite, compared the output with the acceptance criteria "
    "and recorded what it saw. "
)
#: The open work a seat holds in states the ready rule has a clause for, one
#: variant per slot of ``(i // 25) % 10`` among the open issues (8% of all of
#: them). The other slots stay plainly ready.
FLEET_OPEN_VARIANTS = {
    1: "blocked",  # an open blocker: not ready (rule 4)
    2: "yielded_needs_input",  # a needs_input yield nobody answered (rule 6)
    3: "completed_left_open",  # the seat's own completion (rule 6, its seat only)
    4: "yielded_blocked",  # a blocked yield holds nobody back: ready
    5: "archived_open",  # archived while open: in no list
    6: "in_progress",  # in progress, unleased: ready (rule 2's doing category)
    7: "delegated",  # the next seat is a delegated contributor: ready for both
}


@contextlib.contextmanager
def _insert_only_search_index():
    """Index each new row by its INSERT alone, while the fleet seeds.

    ``search.index_document`` deletes a source's old entry before inserting the
    new one, and that DELETE scans the whole full-text table (PERF-5, H2): seeded
    through it, 50,000 issues take about an hour, nearly all of it deleting
    nothing. Every source this seeder indexes is a row it has just created and
    never edits (it changes status and assignee only, which are not indexed), so
    the DELETE removes nothing and the INSERT alone leaves the same index. The
    seed checks that afterwards: no source is indexed twice. This is the seeding
    process only; the server the benchmark starts indexes as the product does."""
    product = search.index_document

    def insert_only(
        conn: sqlite3.Connection, *, kind: str, source_id: int, commit: bool
    ) -> None:
        spec = search._SOURCE[kind]
        cols = (
            spec["body"]
            if spec["title"] is None
            else f"{spec['title']}, {spec['body']}"
        )
        row = conn.execute(
            f"SELECT {cols} FROM {spec['table']} WHERE id = ?", (source_id,)
        ).fetchone()
        title = row[spec["title"]] if spec["title"] is not None else ""
        conn.execute(
            "INSERT INTO search_index (kind, source_id, title, body) "
            "VALUES (?, ?, ?, ?)",
            (kind, source_id, title, row[spec["body"]]),
        )
        if commit:
            conn.commit()

    search.index_document = insert_only
    try:
        yield
    finally:
        search.index_document = product


def _yield_payload(generation: str, reason: str) -> dict:
    return {
        "generation": generation,
        "reason": reason,
        "note": "Seeded: waiting on a decision.",
        "attempted_work": "Reproduced the case and isolated the component.",
        "evidence": ["the focused case reproduces it"],
        "blocking_question": "Which of the two recovery paths should be used?",
        "resume_instructions": "Choose the path, then rerun the focused case.",
    }


def seed_fleet(
    path: Path,
    *,
    issue_count: int,
    seats: int = FLEET_SEATS,
    hot_comments: int = FLEET_HOT_COMMENTS,
    full_index_writes: bool = False,
    analyze: bool = False,
) -> dict:
    """Seed the fleet shape through the application commands; return its map.

    Per issue ``i``: 5% sit in the backlog, 5% in a private project no seat is a
    member of (the operator closes those), the rest in five public projects.
    Of the issues a seat can see, 8% stay open and assigned to a seat (the
    ready set, with the variants in ``FLEET_OPEN_VARIANTS``); the rest are
    worked to the end through the real commands: assigned, claimed, commented
    on by the seat and the operator, completed and closed. Every fiftieth
    issue that is closed is also archived. One open issue of seat 0 carries
    ``hot_comments`` comments, and there is one ~2 kB page per twenty issues.

    What it does not seed: live leases, review requests and candidates,
    project or space members, desk cursors, custom statuses. The rule's
    clauses for those read no rows here; tests/test_ready_open_candidates.py
    builds them.

    The seeding connection uses ``synchronous = OFF`` (per connection, not
    stored), only to make seeding quick; the server opens the file with its own
    settings. ANALYZE runs only with ``analyze`` (see the module docstring), and
    the map says so. ``PATH.json`` holds bearer tokens, so it is written 0600."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    conn = db.connect(path)
    conn.execute("PRAGMA synchronous = OFF")
    db.migrate(conn)
    started = time.perf_counter()
    index_writes = (
        contextlib.nullcontext() if full_index_writes else _insert_only_search_index()
    )
    with index_writes:
        info = _seed_fleet(
            conn, issue_count=issue_count, seats=seats, hot_comments=hot_comments
        )
    twice = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM search_index "
        "GROUP BY kind, source_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if twice:
        raise RuntimeError(f"{twice} sources indexed twice: the seed is not honest")
    info["counts"] = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "issues",
            "comments",
            "activity",
            "notifications",
            "issue_leases",
            "issue_claim_handoffs",
            "pages",
        )
    }
    info["analyzed"] = analyze
    if analyze:
        conn.execute("ANALYZE")
        conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    info["seed_seconds"] = round(time.perf_counter() - started, 1)
    info["db_bytes"] = path.stat().st_size
    _write_private(Path(f"{path}.json"), json.dumps(info, indent=1) + "\n")
    return info


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` readable by its owner alone: it holds bearer tokens."""
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def _seed_fleet(
    conn: sqlite3.Connection, *, issue_count: int, seats: int, hot_comments: int
) -> dict:
    operator = users.get_user(
        conn,
        users.create_user(
            conn,
            email="operator@example.test",
            name="Operator",
            role="admin",
            commit=True,
        )["id"],
    )
    assert operator is not None
    seat_users: list[dict] = []
    for n in range(seats):
        seat = users.get_user(
            conn,
            users.create_user(
                conn,
                email=f"seat{n}@example.test",
                name=f"Seat {n}",
                is_agent=True,
                commit=True,
            )["id"],
        )
        assert seat is not None
        seat_users.append(seat)

    def token(user_id: int, name: str, scopes: tuple[str, ...]) -> str:
        return tokens.create_token(
            conn, user_id=user_id, name=name, scopes=scopes, commit=True
        )["token"]

    info: dict = {
        "shape": "fleet",
        "issues": issue_count,
        "operator": {
            "id": operator["id"],
            "token": token(
                operator["id"],
                "benchmark-operator",
                ("read", "issue:write", "docs:write", "admin"),
            ),
        },
        "seats": [
            {
                "id": seat["id"],
                "token": token(
                    seat["id"], "benchmark-seat", ("read", "issue:write", "docs:write")
                ),
            }
            for seat in seat_users
        ],
    }
    projects = [
        projects_mod.create_project(
            conn,
            name=f"Project {n}",
            key=f"PJ{n}",
            created_by=operator["id"],
            commit=True,
        )
        for n in range(FLEET_PUBLIC_PROJECTS)
    ]
    private = projects_mod.create_project(
        conn, name="Private", key="PRV", created_by=operator["id"], commit=True
    )
    conn.execute(
        "UPDATE projects SET visibility = 'private' WHERE id = ?", (private["id"],)
    )
    conn.commit()
    spaces = [
        spaces_mod.create_space(
            conn,
            key=f"SP{n}",
            name=f"Space {n}",
            created_by=operator["id"],
            commit=True,
        )
        for n in range(2)
    ]
    info["projects"] = [project["id"] for project in projects]
    info["private_project"] = private["id"]

    ready_by_seat: dict[int, list[int]] = {seat["id"]: [] for seat in seat_users}
    variants: dict[str, int] = {}
    issue_ids: list[int] = []
    started = time.perf_counter()
    for i in range(issue_count):
        bucket = i % 20
        if bucket == 0:
            project_id = None
        elif bucket == 1:
            project_id = private["id"]
        else:
            project_id = projects[i % FLEET_PUBLIC_PROJECTS]["id"]
        issue_id = issue_commands.create_issue(
            conn,
            actor=operator,
            title=f"Task {i}: adjust component {i % 211} for case {i % 37}",
            body=FLEET_TEXT * 4
            + f"\nAcceptance: component {i % 211} passes case {i % 37}.",
            priority=issues_mod.PRIORITIES[i % len(issues_mod.PRIORITIES)],
            project_id=project_id,
        )["id"]
        issue_ids.append(issue_id)
        if project_id == private["id"]:
            issue_commands.update_issue(
                conn, actor=operator, issue_id=issue_id, status="done"
            )
        else:
            seat = seat_users[i % seats]
            issue_commands.update_issue(
                conn, actor=operator, issue_id=issue_id, assignee_id=seat["id"]
            )
            if i % 25 in (3, 11):
                variant = FLEET_OPEN_VARIANTS.get((i // 25) % 10, "ready")
                if variant == "blocked" and not ready_by_seat[seat["id"]]:
                    variant = "ready"  # nothing of this seat's to block it yet
                variants[variant] = variants.get(variant, 0) + 1
                _open_variant(
                    conn,
                    operator,
                    seat,
                    issue_id,
                    variant,
                    ready_by_seat,
                    seat_users[(i + 1) % seats],
                )
            else:
                _work_to_the_end(conn, operator, seat, issue_id, i)
                if i % 50 == 7:
                    issue_commands.set_issue_archived(
                        conn, actor=operator, issue_id=issue_id, archived=True
                    )
        if (i + 1) % 2_000 == 0:
            elapsed = time.perf_counter() - started
            print(f"  issues {i + 1}/{issue_count} in {elapsed:.0f}s", file=sys.stderr)

    seat0 = seat_users[0]["id"]
    hot = ready_by_seat[seat0][0] if ready_by_seat[seat0] else issue_ids[0]
    for n in range(hot_comments):
        comment_commands.create_comment(
            conn,
            actor=seat_users[n % seats] if n % 3 else operator,
            issue_id=hot,
            body=f"Thread note {n}. " + FLEET_TEXT * (1 + n % 6),
        )
    info["hot_issue"] = hot
    info["open_variants"] = variants
    info["ready_by_seat"] = {str(k): len(v) for k, v in ready_by_seat.items()}

    page_count = max(50, issue_count // 20)
    with db.transaction(conn, immediate=True):
        for n in range(page_count):
            pages_mod.create_page(
                conn,
                space_id=spaces[n % 2]["id"],
                title=f"Runbook {n}: handling component {n % 211}",
                created_by=operator["id"],
                body=f"# Runbook {n}\n\n"
                + FLEET_TEXT * 18
                + f"\n\nSee #{issue_ids[n % len(issue_ids)]}.",
                commit=False,
            )
    return info


def _claim(conn: sqlite3.Connection, seat: dict, issue_id: int) -> dict:
    issue = issues_mod.get_issue(conn, issue_id)
    assert issue is not None
    return lease_commands.claim_issue(
        conn,
        actor=seat,
        issue_id=issue_id,
        if_match=[issue_etags.current_etag(conn, issue)],
    )


def _work_to_the_end(
    conn: sqlite3.Connection, operator: dict, seat: dict, issue_id: int, i: int
) -> None:
    lease = _claim(conn, seat, issue_id)
    comment_commands.create_comment(
        conn,
        actor=seat,
        issue_id=issue_id,
        body=f"Started on task {i}. " + FLEET_TEXT * 2,
    )
    comment_commands.create_comment(
        conn, actor=operator, issue_id=issue_id, body=f"Noted for task {i}; carry on."
    )
    lease_commands.complete_claim(
        conn, actor=seat, issue_id=issue_id, generation=lease["generation"]
    )
    issue_commands.update_issue(conn, actor=seat, issue_id=issue_id, status="done")


def _open_variant(
    conn: sqlite3.Connection,
    operator: dict,
    seat: dict,
    issue_id: int,
    variant: str,
    ready_by_seat: dict[int, list[int]],
    delegate: dict,
) -> None:
    ready = ready_by_seat[seat["id"]]
    if variant == "blocked":
        # The seat's newest ready issue blocks this one, and stays open.
        issue_commands.link_issues(
            conn,
            actor=operator,
            issue_id=ready[-1],
            target_ref=str(issue_id),
            relation="blocks",
        )
    elif variant in ("yielded_needs_input", "yielded_blocked"):
        lease = _claim(conn, seat, issue_id)
        reason = "needs_input" if variant == "yielded_needs_input" else "blocked"
        lease_commands.yield_claim(
            conn,
            actor=seat,
            issue_id=issue_id,
            **_yield_payload(lease["generation"], reason),
        )
        if variant == "yielded_blocked":
            ready.append(issue_id)
    elif variant == "completed_left_open":
        lease = _claim(conn, seat, issue_id)
        lease_commands.complete_claim(
            conn, actor=seat, issue_id=issue_id, generation=lease["generation"]
        )
    elif variant == "archived_open":
        issue_commands.set_issue_archived(
            conn, actor=operator, issue_id=issue_id, archived=True
        )
    elif variant == "in_progress":
        issue_commands.update_issue(
            conn, actor=operator, issue_id=issue_id, status="in_progress"
        )
        ready.append(issue_id)
    elif variant == "delegated":
        issue_commands.add_contributor(
            conn,
            actor=operator,
            issue_id=issue_id,
            user_id=delegate["id"],
            require_agent=True,
        )
        ready.append(issue_id)
    else:
        ready.append(issue_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="database file to create (overwritten)")
    parser.add_argument(
        "--shape",
        choices=("trail", "fleet"),
        default="trail",
        help="trail (default): issues plus a synthetic event trail; fleet: a "
        "fleet's workspace written through the commands (docs/PERFORMANCE.md)",
    )
    parser.add_argument("--issues", type=int, default=10_000, dest="issue_count")
    parser.add_argument(
        "--events",
        type=int,
        default=None,
        dest="event_count",
        help="trail shape only: the events to record (default 100000)",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="run ANALYZE after seeding. NOT production-shaped; label any number "
        "measured this way as such (the fleet map and the medians file record it)",
    )
    parser.add_argument(
        "--seats", type=int, default=FLEET_SEATS, help="fleet shape: agent accounts"
    )
    parser.add_argument(
        "--hot-comments",
        type=int,
        default=FLEET_HOT_COMMENTS,
        help="fleet shape: comments on seat 0's hot issue",
    )
    parser.add_argument(
        "--full-index-writes",
        action="store_true",
        help="fleet shape: index through the product's delete-then-insert, which "
        "is quadratic in issues (see _insert_only_search_index)",
    )
    args = parser.parse_args(argv)
    if args.shape == "fleet":
        if args.event_count is not None:
            parser.error("--events is for --shape trail: the fleet writes its own")
        info = seed_fleet(
            args.path,
            issue_count=args.issue_count,
            seats=args.seats,
            hot_comments=args.hot_comments,
            full_index_writes=args.full_index_writes,
            analyze=args.analyze,
        )
        summary = ("counts", "open_variants", "seed_seconds", "db_bytes")
        print(json.dumps({key: info[key] for key in summary}))
    else:
        seed(
            args.path,
            issue_count=args.issue_count,
            event_count=100_000 if args.event_count is None else args.event_count,
            analyze=args.analyze,
        )
    print(f"seeded {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
