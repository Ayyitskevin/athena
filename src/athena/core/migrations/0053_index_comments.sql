-- 0053_index_comments: make issue and page comments findable in full-text search.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0054_*.sql.

-- The search_index (0013) already spans issues and pages; comments were the one body
-- of text it did not cover, so discussion was invisible to /search. From here on,
-- core/search.index_document keeps comment entries current (the aegis/mentor comment
-- commands call it on create/edit/delete, in the same transaction as the row + audit
-- event). Comments have no title of their own, so the FTS title is indexed empty and a
-- hit borrows its parent issue/page's title at read time (core/search._enrich).
--
-- Backfill everything that already exists, mirroring 0013's own backfill, so existing
-- comments are searchable the moment this lands rather than only after they are next
-- edited. search_index is a standalone (non-external-content) fts5 table, so a plain
-- INSERT..SELECT is the supported way to add rows.
INSERT INTO search_index (kind, source_id, title, body)
    SELECT 'issue_comment', id, '', body FROM comments;
INSERT INTO search_index (kind, source_id, title, body)
    SELECT 'page_comment', id, '', body FROM page_comments;
