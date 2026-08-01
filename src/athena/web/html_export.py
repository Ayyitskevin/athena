"""The human-readable exit — a space as one standalone HTML document.

JSON portability already answers "can a machine get my data out". This answers
the other half: can a *person* open it in five years with nothing but a browser.
So the output is a single self-contained file — no server, no stylesheet to
fetch, no image that only resolves against a running Athena.

Three rules make it honest.

**It renders through the SAME renderer the pages use.** An export built by a
second renderer would drift from what readers saw, and the drift would only
surface once the original was gone.

**Live things are visibly dead in it.** An embed resolves against a viewer and a
moment; frozen into a file it would be stale data wearing a live face. Exported
embeds render as a refusal box carrying the directive they came from, so a
reader sees both that nothing was resolved and exactly what the author wrote.

**It says what it left out.** Images are inlined so the file stands alone, but
inlining is bounded — a space full of screenshots must not produce a file nobody
can open. Anything skipped is named in place and counted in the footer, because
an export that quietly drops things is worse than one that admits it.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from html import escape as html_escape
import re
import sqlite3

from athena import config
from athena.core import access, attachments
from athena.mentor import pages, spaces
from athena.web.render import render_body

#: Ceilings. An export is a whole space in one file, so every one of these
#: exists to keep "open it in a browser" true rather than theoretical.
MAX_PAGES = 500
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_INLINE_TOTAL_BYTES = 32 * 1024 * 1024

#: The rendered form of an Athena attachment reference. The renderer normalises
#: its own output, so this matches what `render_body` actually emits rather than
#: anything an author typed.
_IMG_SRC_RE = re.compile(r'<img\s+src="/attachments/(\d+)"', re.IGNORECASE)

_STYLE = """
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       line-height: 1.6; max-width: 46rem; margin: 2rem auto; padding: 0 1rem;
       color: #111827; }
h1, h2, h3 { line-height: 1.25; }
a { color: #1d4ed8; }
code, pre { background: #f3f4f6; border-radius: 3px; }
code { padding: 0.1rem 0.3rem; }
pre { padding: 0.6rem 0.8rem; overflow-x: auto; }
img { max-width: 100%; }
.page { border-top: 1px solid #e5e7eb; padding-top: 1.5rem; margin-top: 2.5rem; }
.page-meta { color: #6b7280; font-size: 0.85rem; }
.embed-error { border: 1px dashed #d1d5db; background: #fafafa; padding: 0.6rem
               0.8rem; margin: 0.8rem 0; border-radius: 4px; }
.embed-message { color: #b45309; font-size: 0.85rem; }
.embed-directive { font-size: 0.8rem; white-space: pre-wrap; }
.export-note { color: #6b7280; font-size: 0.85rem; }
.missing-image { border: 1px dashed #d1d5db; padding: 0.4rem 0.6rem;
                 color: #6b7280; font-size: 0.85rem; display: inline-block; }
.toc a { text-decoration: none; }
footer { border-top: 1px solid #e5e7eb; margin-top: 3rem; padding-top: 1rem; }
"""


def _ordered_subtree(rows: list[dict], root_id: int | None) -> list[tuple[int, dict]]:
    """The page tree as a flat depth-annotated list, parents before children.

    Built from an already-fetched flat list rather than by walking the database,
    so the traversal costs no queries and cannot loop: a page whose parent is not
    in the visible set is attached at the root instead of vanishing, because a
    page the reader may see should not be hidden by one they may not.
    """
    by_parent: dict[int | None, list[dict]] = {}
    present = {row["id"] for row in rows}
    for row in sorted(rows, key=lambda item: (item["title"].lower(), item["id"])):
        parent = row["parent_id"] if row["parent_id"] in present else None
        by_parent.setdefault(parent, []).append(row)

    ordered: list[tuple[int, dict]] = []
    seen: set[int] = set()

    def walk(parent: int | None, depth: int) -> None:
        for row in by_parent.get(parent, []):
            if row["id"] in seen:  # defensive: a cycle can never stall the export
                continue
            seen.add(row["id"])
            ordered.append((depth, row))
            walk(row["id"], depth + 1)

    walk(root_id, 0)
    # Anything the walk could not reach still belongs in the file.
    for row in rows:
        if row["id"] not in seen:
            ordered.append((0, row))
    return ordered


def _inline_images(html: str, conn: sqlite3.Connection, budget: list[int]) -> str:
    """Replace attachment image sources with self-contained data URIs.

    ``/attachments/N`` is a live, visibility-gated route: in a file opened from
    disk it resolves to nothing. Inlining the bytes is what makes the document
    genuinely standalone. ``budget`` is a one-element list holding the remaining
    byte allowance, so the cap applies across the WHOLE export rather than per
    image — one enormous page cannot spend everyone else's room silently.
    """

    def replace(match: re.Match[str]) -> str:
        attachment_id = int(match.group(1))
        record = attachments.get(conn, attachment_id)
        if record is None or record["content_type"] not in (
            attachments.INLINE_CONTENT_TYPES
        ):
            return match.group(0)
        if record["byte_size"] > MAX_IMAGE_BYTES or record["byte_size"] > budget[0]:
            return (
                f'<span class="missing-image">Image not included: '
                f"{html_escape(record['filename'])} "
                f'({record["byte_size"]} bytes)</span><img src="'
            )
        stored = attachments.get_stored_name(conn, attachment_id)
        if stored is None:
            return match.group(0)
        try:
            with attachments.open_blob(
                config.ATTACH_DIR, stored, expected_size=record["byte_size"]
            ) as handle:
                data = handle.read()
        except (OSError, ValueError):
            return match.group(0)
        budget[0] -= len(data)
        encoded = base64.b64encode(data).decode("ascii")
        return f'<img src="data:{record["content_type"]};base64,{encoded}"'

    return _IMG_SRC_RE.sub(replace, html)


def build_space_html(
    conn: sqlite3.Connection,
    space_id: int,
    *,
    actor: dict | None,
    root_page_id: int | None = None,
) -> str | None:
    """One space (or one page's subtree) as a standalone HTML document.

    Returns None when the space is not visible, so a caller answers exactly as it
    would for a missing one. ``actor`` is taken from the start rather than
    retrofitted: every body renders as THIS reader sees it, so a cross-link to
    something they cannot read is a dead span in the export exactly as it is on
    the page.
    """
    space = spaces.get_space(conn, space_id)
    if space is None or not access.can_see_space(conn, actor, space_id):
        return None

    rows = pages.list_pages_in_space(conn, space_id)
    ordered = _ordered_subtree(rows, root_page_id)
    if root_page_id is not None:
        root = next((row for row in rows if row["id"] == root_page_id), None)
        if root is None:
            return None
        ordered = [(0, root)] + [
            (depth + 1, row) for depth, row in _ordered_subtree(rows, root_page_id)
        ]

    omitted_pages = max(0, len(ordered) - MAX_PAGES)
    ordered = ordered[:MAX_PAGES]
    budget = [MAX_INLINE_TOTAL_BYTES]

    parts: list[str] = []
    toc: list[str] = []
    for depth, row in ordered:
        page = pages.get_page(conn, row["id"])
        if page is None:
            continue
        anchor = f"page-{page['id']}"
        indent = depth * 1.2
        toc.append(
            f'<li style="margin-left: {indent}rem">'
            f'<a href="#{anchor}">{html_escape(page["title"])}</a></li>'
        )
        body = str(
            render_body(
                conn,
                page["body"],
                actor=actor,
                # No embed results, and the refusal carries the source: an
                # exported embed is visibly dead AND still shows what it said.
                show_refused_directives=True,
            )
        )
        body = _inline_images(body, conn, budget)
        parts.append(
            f'<section class="page" id="{anchor}">'
            f"<h2>{html_escape(page['title'])}</h2>"
            f'<p class="page-meta">Created {html_escape(page["created_at"])}</p>'
            f"{body}</section>"
        )

    exported_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    notes = [
        f"{len(parts)} {'page' if len(parts) == 1 else 'pages'} exported "
        f"{html_escape(exported_at)}.",
        "Embeds are not resolved in an export: each shows the directive it came "
        "from instead of live data.",
        "Only pages you could see when this file was made are included.",
    ]
    if omitted_pages:
        notes.append(
            f"{omitted_pages} further "
            f"{'page was' if omitted_pages == 1 else 'pages were'} not included "
            f"— this export stops at {MAX_PAGES} pages."
        )
    if budget[0] <= 0:
        notes.append("Some images were left out because the file grew too large.")

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html_escape(space['name'])} — Athena export</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<h1>{html_escape(space['name'])}</h1>"
        f'<p class="export-note">Exported from Athena on '
        f"{html_escape(exported_at)}. This file is a snapshot and is not live.</p>"
        f'<nav class="toc"><ul>{"".join(toc)}</ul></nav>'
        f"{''.join(parts)}"
        f'<footer class="export-note">{"<br>".join(notes)}</footer>'
        "</body></html>"
    )
