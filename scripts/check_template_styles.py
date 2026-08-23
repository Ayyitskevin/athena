#!/usr/bin/env python3
"""Contract: every class the app renders has a rule in styles.css, or is
explicitly allowlisted as a deliberate bare hook.

Why this exists: the 2026-08-08 design migration (PR #340) shipped a
stylesheet that silently dropped rules for 116 classes the templates still
referenced. CI stayed green — tests assert on markup, not on whether a class
has styling — and main rendered most surfaces as bare unstyled HTML until a
same-day repair (PR #341). This script makes that failure mode a CI failure.

Two sides of the ledger:

- USED: every literal class token in src/athena/templates/**/*.html, plus
  every class in HTML emitted from Python (src/athena/web/*.py builds embed,
  rollup, mention, and cross-link markup in code — a template-only scan is
  blind to those).
- DEFINED: every class selector in styles.css, with CSS comments stripped
  first, so a class that survives only in a comment ("replaces .card, ...")
  cannot satisfy the contract.

A class that is used, not defined, and not in ALLOWLIST fails the build.
So does a stale ALLOWLIST entry (allowlisted but no longer used, or
allowlisted but now styled): the list must shrink the moment it can.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "src" / "athena" / "templates"
STYLES = REPO / "src" / "athena" / "static" / "styles.css"
WEB_PY = REPO / "src" / "athena" / "web"

#: Classes that are deliberately bare: semantic / JS / test hooks whose
#: styling comes from their context (or from nothing, on purpose). Every
#: entry needs a justification. Remove the entry the day the class gains a
#: rule or leaves the markup — a stale entry fails the build.
ALLOWLIST: dict[str, str] = {
    # Admin surface structure hooks — sectioning only, styled by children.
    "agent-activity-health": "sectioning wrapper on Mission Control",
    "agent-checkin-history": "details disclosure wrapper",
    "agent-health-table": "marker beside .issues-table, which carries the rules",
    "agent-budget-form": "form hook; fields carry the styling",
    "budget-limit-input": "input hook; input element rules apply",
    # Issue-detail / mentor structure hooks — restyled surface-by-surface as
    # the Phosphor Ink adoption continues; bare today as they were pre-redesign.
    "assignee-form": "quick-action form hook",
    "attachments": "sectioning wrapper",
    "backlinks": "sectioning wrapper",
    "comment-edit": "details disclosure wrapper",
    "comment-form": "form hook",
    "comment-list": "list hook; list-element defaults apply",
    "comments": "sectioning wrapper",
    "contributors-block": "marker beside .meta-item, which carries the rules",
    "edit-link": "marker beside .muted, which carries the rules",
    "label-block": "marker beside .panel, which carries the rules",
    "member-name": "inline name span",
    "parent-form": "quick-action form hook",
    "placement-form": "quick-action form hook",
    "priority-form": "quick-action form hook",
    "status-form": "quick-action form hook",
    "sub-issues": "sectioning wrapper",
    # Board filter markers — board-dnd.js selects by [name=]; classes are
    # human-readable markers only.
    "board-filter-project": "marker; .filter-select carries the rules",
    "board-filter-search": "marker; .search-input carries the rules",
    "board-filter-sprint": "marker; .filter-select carries the rules",
    "board-filter-status": "marker; .filter-select carries the rules",
    # SVG surfaces. The scroll wrappers carry real rules now that style-src
    # dropped 'unsafe-inline' and their inline overflow moved into styles.css.
    "timeline-lane": "SVG <g> grouping element",
    # Markdown fence language tag; nh3 strips it before it reaches a page.
    "language-athena": "mentioned in render.py comments only; sanitizer strips it",
    # Embed kind markers — the base .embed rule carries the box; the kind
    # class exists so a future rule can target one kind without new markup.
    "embed-issue": "kind marker; .embed carries the rules",
    "embed-issues": "kind marker; .embed carries the rules",
    "embed-rollup": "kind marker; .embed carries the rules",
}


def _class_tokens(markup: str) -> set[str]:
    """Literal class tokens from HTML-ish text, Jinja stripped first.

    Jinja spans are replaced with a sentinel that cannot appear in a class
    name, NOT with whitespace: a dynamic class like ``rollup-{{ bucket }}``
    must yield nothing (its concrete forms are checked by whoever styles
    them), not a dangling ``rollup-`` literal.
    """
    cleaned = re.sub(r"\{\{[^}]*\}\}|\{%[^%]*%\}|\{#.*?#\}", "\0", markup, flags=re.S)
    tokens: set[str] = set()
    for value in re.findall(r'class="([^"]*)"', cleaned):
        for token in value.split():
            if re.fullmatch(r"[A-Za-z][\w-]*", token):
                tokens.add(token)
    return tokens


def used_classes() -> dict[str, str]:
    """class -> one example source path."""
    used: dict[str, str] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        for token in _class_tokens(path.read_text()):
            used.setdefault(token, str(path.relative_to(REPO)))
    for path in sorted(WEB_PY.glob("*.py")):
        # html_export.py writes a standalone page with its own embedded
        # <style> block — its classes are that stylesheet's contract, not
        # styles.css's.
        if path.name == "html_export.py":
            continue
        # Python builds markup in f-strings with escaped quotes; unescape
        # then reuse the same tokenizer. Comments/docstrings mentioning
        # class= are covered by the allowlist, not special-cased here.
        text = path.read_text().replace('\\"', '"')
        for token in _class_tokens(text):
            used.setdefault(token, str(path.relative_to(REPO)))
    return used


def defined_classes() -> set[str]:
    css = STYLES.read_text()
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return set(re.findall(r"\.([A-Za-z][\w-]*)", css))


def main() -> int:
    used = used_classes()
    defined = defined_classes()

    orphans = {
        cls: src
        for cls, src in used.items()
        if cls not in defined and cls not in ALLOWLIST
    }
    stale = {
        cls: reason
        for cls, reason in ALLOWLIST.items()
        if cls not in used or cls in defined
    }

    if orphans:
        print("Classes rendered by the app with no rule in styles.css:")
        for cls, src in sorted(orphans.items()):
            print(f"  .{cls}  (first seen in {src})")
        print(
            "Style them, remove them from the markup, or allowlist them in "
            "scripts/check_template_styles.py with a justification."
        )
    if stale:
        print("Stale ALLOWLIST entries (no longer used, or now styled):")
        for cls, reason in sorted(stale.items()):
            print(f"  {cls}: {reason}")
        print("Remove them — the allowlist must shrink the moment it can.")
    if orphans or stale:
        return 1

    print(
        f"Athena template styles passed: {len(used)} rendered classes all "
        f"styled or allowlisted ({len(ALLOWLIST)} allowlisted)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
