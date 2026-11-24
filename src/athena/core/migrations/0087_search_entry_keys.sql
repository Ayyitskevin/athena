-- 0087_search_entry_keys: find a source's search entry by key, not by scanning
-- the whole full-text index (spec H2, finding PERF-5).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add a new one.
--
-- search_index (0013) is an FTS5 table whose kind and source_id columns are
-- UNINDEXED, so `DELETE FROM search_index WHERE kind = ? AND source_id = ?` read
-- every entry ever written. Every issue create, edit and comment ran it inside
-- its write transaction. An FTS5 table cannot be given an indexed column, and
-- rebuilding it would rewrite every entry, so this adds an ordinary table that
-- maps each source to its entry's FTS rowid. core/search.index_document is the
-- one writer of both tables from here on, and deletes by rowid.
--
-- What it does to an existing database, in one transaction (the runner wraps
-- this file and its ledger row), in three passes: drop any map row that does
-- not name its own source's entry; record the newest entry of each source; and
-- remove any entry the map then does not name, which is an older duplicate of
-- a mapped source (none in a database only Athena has written: every writer
-- replaced before inserting). Text and the inverted index are not rewritten.
-- Interrupted, it rolls back whole. Run again by hand on a database nothing
-- wrote around the indexer, it finds every source mapped and changes nothing;
-- on one an older process wrote to after this migration, it maps each source
-- to its newest entry again.
--
-- Stop Athena before migrating (OPERATIONS.md). A process still running the
-- previous release writes the index without the map: its entries are not
-- mapped until this file is run again, and until then such a source can show
-- two hits after its next edit.

CREATE TABLE IF NOT EXISTS search_entries (
    kind      TEXT    NOT NULL,
    source_id INTEGER NOT NULL,
    fts_rowid INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (kind, source_id)
) WITHOUT ROWID;

-- A map row that does not name its own source's entry (only possible if the
-- index was written around index_document): the entry is gone, or the rowid
-- now belongs to another source. Left in place it would keep that other
-- source out of the map below, and the last pass would then delete the
-- mapped source's real entry as if it were a duplicate.
DELETE FROM search_entries
WHERE NOT EXISTS (
    SELECT 1 FROM search_index AS entry
     WHERE entry.rowid = search_entries.fts_rowid
       AND entry.kind = search_entries.kind
       AND entry.source_id = search_entries.source_id
);

INSERT OR IGNORE INTO search_entries (kind, source_id, fts_rowid)
    SELECT kind, source_id, MAX(rowid) FROM search_index GROUP BY kind, source_id;

-- An entry the map does not name is an older duplicate of a mapped source: the
-- next write of that source would have removed it, and from here on nothing can.
DELETE FROM search_index
WHERE rowid NOT IN (SELECT fts_rowid FROM search_entries);
