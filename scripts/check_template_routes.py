#!/usr/bin/env python3
"""Contract: every endpoint a template points a browser at exists as a route.

Why this exists: after the 2026-08-08 design migration, "do the buttons
work?" had to be answered by hand — walking every form action and hx-*
attribute against the app's route table. The answer was yes (116/116), but
nothing kept it true. This script does, at CI time: a form action or htmx
verb pointing at a path no router registers fails the build, which is
exactly the "dead button" a user would otherwise find first.

How it matches:

- Template endpoints come from action= and hx-get/post/put/patch/delete=
  attributes. Jinja is normalized, not executed: {{ ... }} becomes a
  wildcard segment, and a {% if %}A{% else %}B{% endif %} inside the
  attribute contributes BOTH arms.
- The route table is the real app's: create_app() against a throwaway
  database, with routers flattened through the app's _IncludedRouter
  wrapper (routes are NOT visible on app.routes directly).
- A wildcard segment matches any single route segment. That looseness is
  deliberate: the contract catches missing/renamed endpoints and wrong
  arity, not wrong parameter values. hx-* attributes check their own verb;
  a plain action= passes with either GET or POST, since the form method
  lives on a different attribute than the one scanned.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "src" / "athena" / "templates"

_ATTR = re.compile(r'(action|hx-get|hx-post|hx-put|hx-patch|hx-delete)="([^"]+)"')
#: Arms may contain {{ ... }} expressions, so they match anything that is not
#: the start of another {% tag %} — a nested {% if %} stays unexpanded rather
#: than matching wrongly.
_BRANCH = re.compile(
    r"\{% if [^%]+%\}((?:(?!\{%).)*)\{% else %\}((?:(?!\{%).)*)\{% endif %\}"
)


def _normalize(raw: str) -> list[str]:
    """Jinja endpoint expression -> concrete candidate paths with wildcards."""
    variants = [raw]
    while any(_BRANCH.search(v) for v in variants):
        nxt = []
        for v in variants:
            m = _BRANCH.search(v)
            if not m:
                nxt.append(v)
                continue
            nxt.append(v[: m.start()] + m.group(1) + v[m.end() :])
            nxt.append(v[: m.start()] + m.group(2) + v[m.end() :])
        variants = nxt
    out = []
    for v in variants:
        v = re.sub(r"\{\{[^}]*\}\}", "\0", v)
        v = re.sub(r"\{%[^%]*%\}", "\0", v)
        v = v.split("?")[0].strip()
        if v.startswith("/"):
            out.append(v)
    return out


def template_endpoints() -> list[tuple[str, str, str]]:
    """(template, method-or-ANY, normalized path) triples."""
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = str(path.relative_to(REPO))
        for attr, value in _ATTR.findall(path.read_text()):
            method = "ANY" if attr == "action" else attr.split("-")[1].upper()
            for candidate in _normalize(value):
                found.append((rel, method, candidate))
    return found


def route_table() -> list[tuple[str, frozenset[str]]]:
    sys.path.insert(0, str(REPO / "src"))
    from athena.main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "contract.db")
    table = []
    for route in app.routes:
        inner = (
            route.original_router.routes
            if type(route).__name__ == "_IncludedRouter"
            else [route]
        )
        for r in inner:
            path = getattr(r, "path", None)
            methods = getattr(r, "methods", None) or set()
            if path:
                table.append((path, frozenset(methods)))
    return table


def _matches(candidate: str, method: str, table) -> bool:
    want = {"GET", "POST"} if method == "ANY" else {method}
    segs = candidate.strip("/").split("/")
    for route_path, methods in table:
        if not (want & methods):
            continue
        rsegs = route_path.strip("/").split("/")
        if len(rsegs) != len(segs):
            continue
        if all(
            rs.startswith("{") or "\0" in ts or rs == ts for rs, ts in zip(rsegs, segs)
        ):
            return True
    return False


def main() -> int:
    table = route_table()
    endpoints = template_endpoints()
    dead = [
        (tpl, method, cand.replace("\0", "{…}"))
        for tpl, method, cand in endpoints
        if not _matches(cand, method, table)
    ]
    if dead:
        print("Template endpoints with no matching route (dead buttons):")
        for tpl, method, cand in dead:
            print(f"  {tpl}: {method} {cand}")
        return 1
    print(
        f"Athena template routes passed: {len(endpoints)} template endpoints "
        f"all reach a registered route ({len(table)} routes)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
