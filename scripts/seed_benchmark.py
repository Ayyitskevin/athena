#!/usr/bin/env python3
"""Seed a benchmark database with a realistic trail, for measuring read performance.

The performance work in docs/OPUS_PERFORMANCE_ADOPTION_GUIDE_ATHENA.md is stated in
measured milliseconds at a named data shape (10k issues / 100k events). This is the
script that builds that shape, so a before/after number in a PR is reproducible by
anyone rather than a claim about a database nobody else has.

    python scripts/seed_benchmark.py /tmp/bench.db
    python scripts/seed_benchmark.py /tmp/small.db --issues 500 --events 5000

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
from pathlib import Path
import sys
import time

if __package__ is None:  # allow running from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from athena.aegis import issues as issues_mod  # noqa: E402
from athena.aegis import projects as projects_mod  # noqa: E402
from athena.core import activity, db, users  # noqa: E402
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
        conn, email="admin@example.test", name="Admin", role="admin"
    )
    owner = users.create_user(conn, email="owner@example.test", name="Owner")
    member = users.create_user(conn, email="member@example.test", name="Member")
    agent = users.create_user(
        conn, email="agent@example.test", name="Agent", is_agent=True
    )

    projects: list[dict] = []
    for i in range(PUBLIC_PROJECTS + PRIVATE_PROJECTS):
        private = i >= PUBLIC_PROJECTS
        project = projects_mod.create_project(
            conn,
            name=f"{'Private' if private else 'Public'} {i}",
            key=f"{'PRV' if private else 'PUB'}{i}",
            created_by=owner["id"],
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="database file to create (overwritten)")
    parser.add_argument("--issues", type=int, default=10_000, dest="issue_count")
    parser.add_argument("--events", type=int, default=100_000, dest="event_count")
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="run ANALYZE after seeding. NOT production-shaped; label any number "
        "measured this way as such.",
    )
    args = parser.parse_args(argv)
    seed(
        args.path,
        issue_count=args.issue_count,
        event_count=args.event_count,
        analyze=args.analyze,
    )
    print(f"seeded {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
