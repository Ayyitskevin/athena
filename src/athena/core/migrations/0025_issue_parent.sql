-- 0025_issue_parent: a parent issue, for epic-lite hierarchy and rollups.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0026.
--
-- An issue may nest under one parent issue (NULL = top-level). This is a single
-- self-referencing column, NOT a separate "epic" type: any issue can be a parent.
-- There is no transition/level machinery — just a tree. SQLite allows ADD COLUMN
-- with a REFERENCES clause only because the default is NULL, so existing rows
-- simply start parentless. No ON DELETE clause (NO ACTION): issues are never
-- deleted in Athena, and the cycle/self guards live at the API boundary
-- (issues.validate_parent) where the FK can't express them.
ALTER TABLE issues ADD COLUMN parent_id INTEGER REFERENCES issues(id);

-- The hot query is "this issue's children" — index the parent end.
CREATE INDEX idx_issues_parent ON issues (parent_id);
