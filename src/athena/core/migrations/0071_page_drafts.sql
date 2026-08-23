-- 0071_page_drafts: an author's unsaved work in progress, per user per page.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0072_*.sql.
--
-- A browser crash, a closed laptop, or a mis-clicked Cancel currently costs an
-- author everything they typed since the last Save. This table is the answer,
-- and its whole design is an ownership boundary:
--
-- A DRAFT IS USER-PRIVATE STATE, NOT CONTENT. That single sentence decides
-- everything else. Content is what a page IS — versioned, attributed, indexed,
-- exported, and readable by everyone who can see the space. A draft is what one
-- person happens to have typed and not yet committed. So a draft:
--
--   * lives in its OWN table. Every existing write to page content funnels
--     through page_commands.edit_page -> pages.update_page, which unconditionally
--     snapshots a version. There is no write path to a page that skips
--     versioning, and there must not be one — so a draft simply is not a write
--     to the page.
--   * records NO activity event. Autosave firing every few seconds is not a
--     lifecycle moment; recording it would drown the trail it is supposed to
--     inform, fan out to every watcher, and land in undo as an unclassified
--     verb. "The audit trail gains nothing until a human commits."
--   * is readable ONLY by its owner. Not by admins, not by space members. It is
--     unfinished thinking, and the space's visibility rules govern the PAGE, not
--     one person's scratch copy of it.
--   * is not exported. Portability bundles carry pages, versions, comments and
--     labels — the content — exactly as they already omit saved filters and
--     watches. A draft leaving in a bundle would be someone's private notebook
--     travelling inside a document.
--   * never becomes the page by itself. Only an explicit save through the
--     ordinary audited command turns a draft into content.
--
-- One row per (page, author): a draft is "where I am on this page", not a
-- history. Saving again overwrites it, and an explicit save or discard removes
-- it. ON DELETE CASCADE both ways, mirroring page_labels (0037): a purged page
-- or a removed user must not leave a private ghost behind.

CREATE TABLE page_drafts (
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL DEFAULT '' CHECK (length(title) <= 500),
    -- Bounded like every other stored text. The cap matches the render/preview
    -- ceiling, so anything an author can preview is something they can draft.
    body       TEXT    NOT NULL DEFAULT '' CHECK (length(body) <= 200000),
    -- The page's ETag when this draft was last taken. It is what makes a stale
    -- draft VISIBLE: if someone else saved in between, restoring blindly would
    -- silently clobber their work, so the surface compares this against the
    -- page's current ETag and says so instead.
    based_on   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (page_id, owner_id)
);

-- "What am I part-way through?" — one author's drafts, freshest first.
CREATE INDEX idx_page_drafts_owner ON page_drafts (owner_id, updated_at DESC);
