-- 0013_search: unified full-text search across issues and pages — the second
-- one-roof payoff (after links). FORWARD-ONLY: once applied anywhere, never edit
-- this file — add 0014_*.sql.

-- One FTS5 index spanning BOTH Aegis issues and Mentor pages, so a single query
-- ranks hits from either side together (Confluence/Jira global search). It is a
-- DERIVED index, not a source of truth: the `issues`/`pages` rows remain the
-- truth, and core/search.index_document re-derives a row's entry on every write
-- (mirroring how core/links.sync_links maintains the cross-link index). We use a
-- STANDALONE fts5 table (it keeps its own copy of the text) rather than an
-- external-content one so the sync stays a plain function call, matching the
-- links pattern — no triggers, no content-table coupling.
--
-- kind + source_id are UNINDEXED: they are returned and filtered on, but must not
-- be tokenized (we never want to free-text match the word "issue" or a row id).
-- title and body ARE indexed; title is listed first so a title hit can be weighted
-- above a body hit at query time (bm25 column weights).
CREATE VIRTUAL TABLE search_index USING fts5(
    kind UNINDEXED,
    source_id UNINDEXED,
    title,
    body
);

-- Backfill everything that already exists, so search works the moment this
-- migration lands rather than only for rows written afterward. New rows are kept
-- current by index_document from the data-access layer.
INSERT INTO search_index (kind, source_id, title, body)
    SELECT 'issue', id, title, body FROM issues;
INSERT INTO search_index (kind, source_id, title, body)
    SELECT 'page', id, title, body FROM pages;
