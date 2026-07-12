-- 0045_issue_filter_indexes: back the hot issue filters + the visibility gate.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0046.
--
-- list_issues filters on status / assignee_id / project_id (aegis/issues.py:537-554),
-- and EVERY visibility-gated read narrows with `project_id IN (...)` (core/access.py via
-- issues.py:564-572). Yet only sprint_id, parent_id, and archived_at were indexed, so
-- each board, dashboard, and search visibility check did a full scan of the issues table.
-- These three indexes back the dominant filter and gate predicates. The write cost is
-- negligible at the solo/tiny-team scale Athena targets; the read win grows with the
-- issue count. project_id is listed first because the visibility gate touches it on
-- nearly every read.
CREATE INDEX idx_issues_project ON issues (project_id);
CREATE INDEX idx_issues_assignee ON issues (assignee_id);
CREATE INDEX idx_issues_status ON issues (status);
