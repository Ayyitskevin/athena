#!/usr/bin/env python3
"""Fail when Athena's surface grows or shrinks without a line saying why (B6b).

``docs/PRUNE_LEDGER.md`` asks every subsystem to justify its keep, but nothing
made an addition say what it earns, and the ledger's own counts went stale
(362 routes, 124 tools and 65 tables in August, against 417, 146 and 73 when
this check was written). ``scripts/surface_counts.toml`` keeps a baseline and one
``[[change]]`` per later change to a count; this check measures the three and
fails unless the baseline plus every change's deltas equals the measurement, and
fails on a change that adds without ``earns``.

What is counted (the TOML's header says the same):

- ``routes``: every route the app registers, flattened through included routers
  (``agent_openapi.route_contexts``, as ``check_template_routes.py`` reads them);
- ``tools``: every MCP tool declared (``mcp.server.declared()``);
- ``tables``: every table and virtual table of a freshly migrated database
  (``PRAGMA table_list``), without SQLite's own and shadow tables.

Usage::

    python scripts/check_surface_counts.py [--ledger scripts/surface_counts.toml]
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
import tomllib

REPO = Path(__file__).resolve().parents[1]
LEDGER = Path(__file__).resolve().with_name("surface_counts.toml")
KINDS = ("routes", "tools", "tables")


@dataclass(frozen=True)
class Ledger:
    baseline: dict[str, int]
    changes: list[dict]


def measure() -> dict[str, int]:
    """The three counts of this checkout."""
    sys.path.insert(0, str(REPO / "src"))
    from athena import agent_openapi
    from athena.core import db
    from athena.main import create_app
    from athena.mcp import server as mcp_server

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "surface.db")
        routes = sum(
            1
            for route in agent_openapi.route_contexts(app)
            if getattr(route, "path", None)
        )
        conn = db.connect(Path(tmp) / "tables.db")
        try:
            db.migrate(conn)
            tables = sum(
                1
                for row in conn.execute("PRAGMA table_list")
                if row["schema"] == "main"
                and row["type"] in ("table", "virtual")
                and not str(row["name"]).startswith("sqlite_")
            )
        finally:
            conn.close()
    return {"routes": routes, "tools": len(mcp_server.declared()), "tables": tables}


def read_ledger(path: Path) -> Ledger:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    baseline = data.get("baseline", {})
    return Ledger(
        baseline={kind: baseline[kind] for kind in KINDS if kind in baseline},
        changes=list(data.get("change", [])),
    )


def check(measured: Mapping[str, int], ledger: Ledger) -> list[str]:
    """Every way the ledger fails to account for ``measured``."""
    problems: list[str] = []
    for kind in KINDS:
        if not isinstance(ledger.baseline.get(kind), int):
            problems.append(f"[baseline] has no integer {kind}")
    for index, change in enumerate(ledger.changes, start=1):
        name = change.get("change")
        where = f"[[change]] {index}" + (f" ({name})" if name else "")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where} names no `change` (the requirement or PR)")
        unknown = set(change) - {"change", "earns", *KINDS}
        if unknown:
            problems.append(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
        deltas = {kind: change.get(kind, 0) for kind in KINDS}
        if any(not isinstance(value, int) for value in deltas.values()):
            problems.append(f"{where} has a delta that is not an integer")
            continue
        if not any(deltas.values()):
            problems.append(f"{where} changes no count")
        earns = change.get("earns")
        if any(value > 0 for value in deltas.values()) and (
            not isinstance(earns, str) or not earns.strip()
        ):
            problems.append(
                f"{where} adds to the surface without `earns`: say what the "
                "addition is for"
            )
    if problems:
        return problems
    for kind in KINDS:
        accounted = ledger.baseline[kind] + sum(
            change.get(kind, 0) for change in ledger.changes
        )
        if measured[kind] != accounted:
            delta = measured[kind] - accounted
            problems.append(
                f"{kind}: measured {measured[kind]}, the ledger accounts for "
                f"{accounted}; add a [[change]] to scripts/surface_counts.toml with "
                f"{kind} = {delta:+d}"
                + (" and `earns`, what the addition is for" if delta > 0 else "")
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args(argv)
    measured = measure()
    problems = check(measured, read_ledger(args.ledger))
    if problems:
        print(f"Athena surface counts failed: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        "Athena surface counts passed: "
        + ", ".join(f"{measured[kind]} {kind}" for kind in KINDS)
        + ", every change accounted for"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
