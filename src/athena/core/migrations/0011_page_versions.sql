-- 0011_page_versions: edit history for Mentor pages (Confluence-style).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0012_*.sql.

-- The live `pages` row only knows who CREATED it. To attribute each historical
-- snapshot to whoever last wrote it, the page must also carry its current
-- revision's editor + time. SQLite forbids a non-constant default (datetime('now'))
-- on ADD COLUMN, so these go on nullable and we backfill from the creation
-- fields — at this point every existing page is still on its original revision,
-- so created_* IS the current revision. From here on, create_page and update_page
-- always set these, so live rows are never null.
ALTER TABLE pages ADD COLUMN updated_by INTEGER REFERENCES users(id);
ALTER TABLE pages ADD COLUMN updated_at TEXT;
UPDATE pages SET updated_by = created_by, updated_at = created_at;

-- A superseded revision of a page. On edit, the page's CURRENT content is copied
-- here BEFORE the new content overwrites it — so this table is the page's past
-- and the live `pages` row is its present. `version` is 1-based and dense per
-- page: version 1 is the text the page was created with, exposed only once a
-- second revision supersedes it. No ON DELETE (NO ACTION): there is no page
-- delete path, and history must never point at a ghost page.
CREATE TABLE page_versions (
    id         INTEGER PRIMARY KEY,
    page_id    INTEGER NOT NULL REFERENCES pages(id),
    version    INTEGER NOT NULL,                       -- 1-based sequence within the page
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    edited_by  INTEGER NOT NULL REFERENCES users(id),  -- author of THIS snapshot's content
    created_at TEXT NOT NULL,                          -- when this content became live (copied from the page)
    UNIQUE (page_id, version)
);

-- The hot query is "all versions of this page" — index the page.
CREATE INDEX idx_page_versions_page ON page_versions (page_id);
