-- 0035_issue_archive: soft-delete / archive for issues.
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0036_*.sql.
--
-- Issues are never destroyed (deletion is refused by design, for audit integrity),
-- so closed/obsolete ones pile up in every list and board forever. archived_at is
-- the soft-delete: a non-NULL timestamp hides the issue from the default lists
-- while preserving the row and its whole history. The row is the audit record; the
-- archive verb is itself recorded on the activity trail.

ALTER TABLE issues ADD COLUMN archived_at TEXT;

-- The default list filters "archived_at IS NULL"; index it so that stays cheap as
-- the archived set grows.
CREATE INDEX idx_issues_archived ON issues (archived_at);
