"""Mentor page tools.

Registered by mcp.server.build_server."""

from __future__ import annotations

from athena.mcp.server import (
    IdempotencyKey,
)


def register_page_tools(tool, mutation_tool, client) -> None:
    # --- pages (Mentor) -----------------------------------------------------

    @tool
    def list_spaces() -> list:
        """List Mentor spaces (id, key, name)."""
        return client.list_spaces()

    @tool
    def list_pages(space_id: int, include_archived: bool = False) -> list:
        """List the pages in a space. Archived (soft-deleted) pages are hidden by
        default; pass include_archived=true to see them."""
        return client.list_pages(space_id, include_archived=include_archived)

    @tool
    def get_page(page_id: int) -> dict:
        """Get one Mentor page (title + Markdown body). The response includes the
        server's opaque ETag as _etag; copy it exactly into if_match on a guarded
        update_page to make the edit fail rather than clobber a concurrent change."""
        return client.get_page(page_id)

    @tool
    def find_pages_by_title(title: str, space_id: int | None = None) -> list:
        """Find Mentor pages by their TITLE instead of a numeric id — the address you can
        recall without a lookup (numeric ids are exactly what an agent is worst at).
        Returns every exact, case-insensitive title match: [] if none, one for the common
        case, or several when a title is reused across spaces (pass space_id to narrow to
        one). Archived pages, and pages in spaces you can't see, are omitted. Use it to
        turn a remembered title into a page id for get_page / update_page."""
        return client.find_pages_by_title(title, space_id=space_id)

    @tool
    def page_backlinks(page_id: int) -> list:
        """What references this page — the INCOMING edges of the knowledge graph. Each
        item is {kind, id, title, exists}: another issue or page whose body cross-links
        here. Results the caller may not see (private space/project) are hidden. Use it
        to find what depends on a doc before editing it."""
        return client.page_backlinks(page_id)

    @tool
    def page_outgoing_links(page_id: int) -> list:
        """What this page references — the OUTGOING edges of the knowledge graph (the
        [[issue:N]]/[[page:N]] cross-links in its body). Each item is {kind, id, title,
        exists}; exists=false marks a broken link (target deleted or never created).
        Use it to walk from a doc to the things it points at."""
        return client.page_outgoing_links(page_id)

    @tool
    def list_page_versions(page_id: int) -> list:
        """The page's superseded revisions, newest first (the live page is NOT one of
        them — get it with get_page). Each is {id, page_id, version, title, body,
        edited_by, created_at}. Pair with restore_page_version to roll back."""
        return client.list_page_versions(page_id)

    @tool
    def get_page_version(page_id: int, version: int) -> dict:
        """Fetch one historical page revision by its version number (title + body as of
        that revision), for diffing against the live page or another version."""
        return client.get_page_version(page_id, version)

    @mutation_tool
    def restore_page_version(
        page_id: int, version: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Restore a page's content to a prior version. Non-destructive: the current
        content is snapshotted into history first, so a restore is itself reversible.
        Returns the restored (now-live) page."""
        return client.restore_page_version(
            page_id, version, idempotency_key=idempotency_key
        )

    @mutation_tool
    def create_page(
        space_id: int,
        title: str,
        body: str = "",
        parent_id: int | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create a Mentor page in a space. Optionally nest it under parent_id (a
        page in the same space). Bodies support Markdown and cross-links."""
        return client.create_page(
            space_id=space_id,
            title=title,
            body=body,
            parent_id=parent_id,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def update_page(
        page_id: int,
        title: str | None = None,
        body: str | None = None,
        if_match: str | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Update a page's title and/or body. Each edit is snapshotted into the
        page's version history automatically. Pass if_match with the page's current
        ETag (from get_page) for an optimistic-lock edit: if another agent changed the
        page since you read it, the edit fails with a 412 instead of clobbering their
        write — the shared-memory-safe way for concurrent agents to edit one page."""
        return client.update_page(
            page_id,
            title=title,
            body=body,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @mutation_tool
    def archive_page(
        page_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Archive (soft-delete) a page: it's hidden from the space tree, navigation,
        and search, but the page — with its full version history and comments — is
        kept and can be restored. The non-destructive alternative to deleting a page.
        Returns the page."""
        return client.archive_page(page_id, idempotency_key=idempotency_key)

    @mutation_tool
    def unarchive_page(
        page_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Restore a previously archived page to the active tree/nav/search. Returns
        the page."""
        return client.unarchive_page(page_id, idempotency_key=idempotency_key)

    @mutation_tool
    def label_page(
        page_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Attach an existing label (by id — see list_labels) to a Mentor page.
        Idempotent — the same shared vocabulary issues use. Returns the page."""
        return client.label_page(page_id, label_id, idempotency_key=idempotency_key)

    @mutation_tool
    def unlabel_page(
        page_id: int, label_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict:
        """Remove a label from a Mentor page. Returns the page."""
        return client.unlabel_page(page_id, label_id, idempotency_key=idempotency_key)
