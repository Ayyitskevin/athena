-- 0051_pages_title_index: title-based addressing. [[Page Title]] wiki-links and the
-- fetch-by-title lookup resolve a page by its title, case-insensitively. Index the
-- title column under COLLATE NOCASE so that resolution — run on EVERY issue/page write
-- via links.sync_links, and on every by-title read — is an index probe, not a full
-- table scan of a growing wiki. FORWARD-ONLY: once applied anywhere, never edit this
-- file — add 0052_*.sql.
CREATE INDEX idx_pages_title ON pages (title COLLATE NOCASE);
