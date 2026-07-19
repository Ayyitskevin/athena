-- 0050_page_archive: soft-delete / archive for Mentor pages.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0051_*.sql.
--
-- Deleting a page destroys its history and its comments irreversibly
-- (mentor/pages.delete_page), so an obsolete page had only the nuclear option and
-- no reversible "get it out of the way". archived_at is the soft-delete, mirroring
-- issues (0035_issue_archive): a non-NULL timestamp hides the page from the default
-- space tree, the navigation rail, and search, while preserving the row, its whole
-- version history, and its comments. Clearing it restores the page. The row is the
-- record; the archive verb is itself recorded on the activity trail.

ALTER TABLE pages ADD COLUMN archived_at TEXT;

-- The default page list filters "archived_at IS NULL"; index it so that stays cheap
-- as the archived set grows.
CREATE INDEX idx_pages_archived ON pages (archived_at);
