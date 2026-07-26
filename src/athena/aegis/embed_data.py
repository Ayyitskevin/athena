"""Resolving embed directives to real data, for whoever is looking.

`core/embeds.py` parses the directive; this runs it. The split keeps the grammar
pure and testable, and — more importantly — means the HTML renderer and the
structured MCP read resolve through the **same** function. An agent reading a
page and a human viewing it see the same rows or the same refusal, because there
is one resolver, not one per surface.

Two rules are the whole module.

**Visibility is the viewer's, never the author's.** An embed written by an admin
renders, for a member, only the issues that member could already see. This falls
out of routing through `issue_query`, which composes the visibility clause into
its SQL — but it is stated here because it is the property most likely to be
broken by a future "optimization" that resolves once and caches the result.

**A directive that cannot render says so.** Every failure path returns a result
carrying an ``error`` string, and the surface renders that visibly. An embed that
silently produced nothing would be indistinguishable from one that matched
nothing, which is the same invented answer the query grammar refuses to give.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import re

from athena.aegis import issue_query, issues
from athena.core import access, embeds, links, work_query
from athena.core.embeds import Directive

#: A bare project-key reference (ATH-12) — the [[…]]-less form an embed writes.
#: Built from links.KEY_REF_RE's own pattern so the two stay the same grammar.
_KEY_REF = re.compile(r"([A-Za-z][A-Za-z0-9]*)-(\d+)")


def _error(directive: Directive | None, message: str) -> dict[str, Any]:
    return {
        "kind": directive.kind if directive else None,
        "title": directive.title if directive else "",
        "error": message,
    }


def _issue_summary(row: dict) -> dict[str, Any]:
    """The bounded projection an embed shows.

    Deliberately not the whole issue: an embed is a pointer to work, and copying
    every field (body included) into every page view would make a page a second,
    staler copy of the issue list.
    """
    return {
        "id": row["id"],
        "key": row.get("key"),
        "title": row["title"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee_name": row.get("assignee_name"),
        "assignee_is_agent": row.get("assignee_is_agent"),
        "project_key": row.get("project_key"),
    }


def resolve(
    conn: sqlite3.Connection, directive: Directive, *, actor: dict | None
) -> dict[str, Any]:
    """Resolve one directive against the caller's visibility.

    Never raises for a directive-level problem: a bad query, an unresolvable
    issue, or a refused atom comes back as an ``error`` the surface renders in
    place. A page must not fail to load because one embed was mistyped.
    """
    visible = access.visible_project_filter(conn, actor)

    if directive.kind == "issue":
        return _resolve_issue(conn, directive, actor=actor, visible=visible)

    try:
        parsed = work_query.parse(directive.query)
    except work_query.QueryError as exc:
        return _error(directive, str(exc))

    try:
        if directive.kind == "count":
            matched = issue_query.count_query(
                conn, parsed, actor=actor, visible_project_ids=visible
            )
            return {
                "kind": "count",
                "title": directive.title,
                "query": parsed.raw,
                "matched": matched,
                "error": None,
            }

        rows = issue_query.run_query(
            conn,
            parsed,
            actor=actor,
            visible_project_ids=visible,
            limit=directive.limit,
        )
        matched = issue_query.count_query(
            conn, parsed, actor=actor, visible_project_ids=visible
        )
    except issue_query.QueryCompileError as exc:
        return _error(directive, str(exc))

    # The count is computed even though the rows are bounded, so the surface can
    # say "10 of 42" rather than implying the page is the whole answer. Silent
    # truncation is how an operator concludes there are ten open issues when
    # there are forty-two.
    return {
        "kind": "issues",
        "title": directive.title,
        "query": parsed.raw,
        "items": [_issue_summary(row) for row in rows],
        "matched": matched,
        "shown": len(rows),
        "truncated": matched > len(rows),
        "error": None,
    }


def _resolve_issue(
    conn: sqlite3.Connection,
    directive: Directive,
    *,
    actor: dict | None,
    visible: set[int] | None,
) -> dict[str, Any]:
    """One issue, by id or project key.

    A hidden issue is reported exactly like a missing one. Distinguishing them
    would make an embed an existence oracle for private work — the same rule the
    cross-link renderer already applies to `[[ATH-12]]`.
    """
    ref = directive.issue_ref.strip()
    issue_id: int | None = None
    parsed_id = issues.parse_filter_id(ref)
    if issues.is_filter_id(parsed_id):
        issue_id = parsed_id
    else:
        # A project key like ATH-12. Reuses the SAME resolver the [[ATH-12]]
        # cross-link renderer uses, so an embed and a wikilink can never disagree
        # about which issue a key names.
        match = _KEY_REF.fullmatch(ref)
        if match is not None:
            keyed = links.resolve_key_ref(conn, match.group(1), int(match.group(2)))
            if keyed["exists"]:
                issue_id = int(keyed["id"])

    if issue_id is None or not access.can_see_issue(conn, actor, issue_id):
        return _error(directive, f"no issue matches {ref!r}")
    row = issues.get_issue(conn, issue_id)
    if row is None:
        return _error(directive, f"no issue matches {ref!r}")
    del visible  # visibility for a single issue is can_see_issue, checked above
    return {
        "kind": "issue",
        "title": directive.title,
        "item": _issue_summary(row),
        "error": None,
    }


def resolve_body(
    conn: sqlite3.Connection, text: str | None, *, actor: dict | None
) -> list[dict[str, Any]]:
    """Resolve every directive in a body, in order, bounded per page.

    Directives beyond :data:`embeds.MAX_DIRECTIVES_PER_PAGE` are not silently
    dropped — they resolve to a visible refusal, so the author learns the page hit
    the ceiling instead of wondering why the last block vanished.
    """
    resolved: list[dict[str, Any]] = []
    for index, (_whole, body) in enumerate(embeds.find_directives(text)):
        if index >= embeds.MAX_DIRECTIVES_PER_PAGE:
            resolved.append(
                _error(
                    None,
                    "this page has more than "
                    f"{embeds.MAX_DIRECTIVES_PER_PAGE} embeds; "
                    "this one was not rendered",
                )
            )
            continue
        try:
            directive = embeds.parse_directive(body)
        except embeds.DirectiveError as exc:
            resolved.append(_error(None, str(exc)))
            continue
        resolved.append(resolve(conn, directive, actor=actor))
    return resolved
