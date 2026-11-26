"""Mentor page tools.

Registered by mcp.server.build_server."""

from __future__ import annotations

from athena.core import tokens
from athena.mcp import results
from athena.mcp.results import ListResult, listing
from athena.mcp.server import (
    ADDS,
    FULL_ONLY,
    KNOWLEDGE,
    READ_ONLY,
    SETS,
    BodyOffset,
    IdempotencyKey,
    ListCursor,
    PageListLimit,
)


def register_page_tools(tool, mutation_tool, client) -> None:
    # --- pages (Mentor) -----------------------------------------------------
    # Every page edit is snapshotted into the page's version history, so no page
    # write is destructive.

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_spaces() -> ListResult:
        """List Mentor spaces (id, key, name)."""
        return listing(client.list_spaces())

    # A write with shared guidance; find-or-create needs no idempotency key.
    # In the knowledge profile (G8): the desk shows a notebook's first lines and
    # says how to make one, so a seat that can edit pages can open its own.
    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=KNOWLEDGE, hints=SETS)
    def open_my_notebook() -> dict:
        """Open your notebook, notes kept between sessions that only you and
        the operator see, creating it on first use: {page, created}. Append
        with update_page and if_match = its _etag. Takes no idempotency key: a
        repeat returns the same page. get_page with body_offset reads a cut
        body on."""
        return client.open_my_notebook()

    @tool(scope=READ_ONLY, profiles=KNOWLEDGE)
    def list_pages(
        space_id: int,
        include_archived: bool = False,
        limit: PageListLimit = 50,
        cursor: ListCursor | None = None,
    ) -> ListResult:
        """A space's pages by title, without bodies; archived ones only with
        include_archived=true."""

        def fetch(rows: int) -> results.CursorPage:
            page = client.list_pages(
                space_id, include_archived=include_archived, limit=rows, cursor=cursor
            )
            return page["pages"], page["next_cursor"], page["has_more"]

        return results.cursor_page(fetch, limit=limit)

    @tool(scope=READ_ONLY, profiles=KNOWLEDGE)
    def get_page(page_id: int, body_offset: BodyOffset = 0) -> dict:
        """Get one page. Its `_etag` is the if_match a guarded update_page
        takes. A long body is cut; body_offset reads on."""
        return results.from_offset(client.get_page(page_id), "body", body_offset)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def find_pages_by_title(title: str, space_id: int | None = None) -> ListResult:
        """Find Mentor pages by their TITLE instead of a numeric id — the address you can
        recall without a lookup (numeric ids are exactly what an agent is worst at).
        Returns every exact, case-insensitive title match: [] if none, one for the common
        case, or several when a title is reused across spaces (pass space_id to narrow to
        one). Archived pages, and pages in spaces you can't see, are omitted. Use it to
        turn a remembered title into a page id for get_page / update_page."""
        rows = client.find_pages_by_title(title, space_id=space_id)
        return listing([results.summary(row) for row in rows])

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def page_backlinks(page_id: int) -> ListResult:
        """What references this page — the INCOMING edges of the knowledge graph. Each
        item is {kind, id, title, exists}: another issue or page whose body cross-links
        here. Results the caller may not see (private space/project) are hidden. Use it
        to find what depends on a doc before editing it."""
        return listing(client.page_backlinks(page_id))

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def page_outgoing_links(page_id: int) -> ListResult:
        """What this page references — the OUTGOING edges of the knowledge graph (the
        [[issue:N]]/[[page:N]] cross-links in its body). Each item is {kind, id, title,
        exists}; exists=false marks a broken link (target deleted or never created).
        Use it to walk from a doc to the things it points at."""
        return listing(client.page_outgoing_links(page_id))

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_page_versions(page_id: int) -> ListResult:
        """The page's superseded revisions, newest first (the live page is NOT one of
        them — get it with get_page). Each is {id, page_id, version, title,
        edited_by, created_at}; get_page_version has the body. Pair with
        restore_page_version to roll back."""
        return listing(
            [results.summary(row) for row in client.list_page_versions(page_id)]
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_page_version(page_id: int, version: int) -> dict:
        """Fetch one historical page revision by its version number (title + body as of
        that revision), for diffing against the live page or another version."""
        return client.get_page_version(page_id, version)

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def restore_page_version(
        page_id: int, version: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Restore a page's content to a prior version. Non-destructive: the current
        content is snapshotted into history first, so a restore is itself reversible.
        Returns the restored (now-live) page."""
        return client.restore_page_version(
            page_id, version, idempotency_key=idempotency_key
        )

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=KNOWLEDGE, hints=ADDS)
    def create_page(
        space_id: int,
        title: str,
        body: str = "",
        parent_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create a page in a space, under parent_id if given. Bodies take
        Markdown and cross-links."""
        return client.create_page(
            space_id=space_id,
            title=title,
            body=body,
            parent_id=parent_id,
            idempotency_key=idempotency_key,
        )

    # Not idempotent: each call cuts a version, even unchanged text.
    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=KNOWLEDGE, hints=ADDS)
    def update_page(
        page_id: int,
        title: str | None = None,
        body: str | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Update a page's title or body; each edit keeps a version. Pass
        if_match (get_page's `_etag`) so a concurrent edit fails 412 instead of
        being clobbered."""
        return client.update_page(
            page_id,
            title=title,
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def archive_page(
        page_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Archive (soft-delete) a page: it's hidden from the space tree, navigation,
        and search, but the page — with its full version history and comments — is
        kept and can be restored. The non-destructive alternative to deleting a page.
        Returns the page."""
        return client.archive_page(page_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def unarchive_page(
        page_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Restore a previously archived page to the active tree/nav/search. Returns
        the page."""
        return client.unarchive_page(page_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def label_page(
        page_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Attach an existing label (by id — see list_labels) to a Mentor page.
        Idempotent — the same shared vocabulary issues use. Returns the page."""
        return client.label_page(page_id, label_id, idempotency_key=idempotency_key)

    @mutation_tool(scope=tokens.DOCS_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def unlabel_page(
        page_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Remove a label from a Mentor page. Returns the page."""
        return client.unlabel_page(page_id, label_id, idempotency_key=idempotency_key)
