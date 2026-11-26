-- 0096_issue_idle_key: store what the idle sort orders by, so `sort:idle-desc`
-- reads an index instead of every matching issue's trail (H5).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add 0096_*.sql.
--
-- The work query's idle key was COALESCE(last work, created_at), where "last
-- work" is the newest NATIVE activity row on the issue whose verb is in
-- work_progress.SUBSTANTIVE_WORK_VERBS (work_progress.last_work_sql). It was
-- computed per matching issue and the whole set sorted before the page was
-- taken: about 8.0 M SQLite steps at 50,000 issues. No index could order it,
-- and `is:open sort:idle-desc` also had to know each issue's status category,
-- which lives in a join.
--
-- Two stored columns, each kept by triggers, so it can never disagree with
-- the tables it is derived from, whatever writes them (the recorder, a
-- portability import, an operator's sqlite3 shell, a test back-dating rows):
--
--   * issues.last_work_at -- exactly last_work_sql's value: MAX(created_at)
--     over the issue's activity rows with target_kind = 'issue', imported_at
--     IS NULL and a substantive work verb; NULL when there are none. The
--     idle anchor stays COALESCE(last_work_at, created_at), so creation still
--     counts when nothing was ever recorded, and an issue's created_at edit is
--     followed by the expression index itself.
--   * issues.status_closed -- 1 when the issue's status resolves to the
--     `done` category (statuses.category_sql: the project's own status row,
--     else the default mapping), else 0. It is what `is:open`/`is:closed`
--     read, and it is the leading key of the open/closed idle index.
--
-- The work verb list and the default status mapping are Python constants;
-- they are written out here because a trigger cannot call Python. Each copy
-- is pinned to its constant by tests/test_issue_idle_key.py, so a change to
-- either fails the suite until a new migration rewrites these triggers.
--
-- Why columns and not a side table: the sort's key also needs created_at
-- and the archived filter, which live on issues; an index can only span one
-- table. Why triggers and not the recorder: activity.record is not the only
-- writer of the trail's rows (imports pass through it, but the status and
-- project writers that change open/closed do not, and tests and operators
-- edit rows directly), and a trigger runs inside the very statement that
-- changes its inputs, so the stored value commits or rolls back with them.
-- 0055's reservations already make a new issue's trail empty, so an issue
-- is born with no last work.

ALTER TABLE issues ADD COLUMN last_work_at TEXT;
ALTER TABLE issues ADD COLUMN status_closed INTEGER NOT NULL DEFAULT 0
    CHECK (status_closed IN (0, 1));

-- Backfill: the same MAX the query computed, one seek of idx_activity_target
-- per issue, and the same category resolution the query joined.
UPDATE issues SET last_work_at = (
    SELECT MAX(wa.created_at) FROM activity wa
     WHERE wa.target_kind = 'issue' AND wa.target_id = issues.id
       AND wa.imported_at IS NULL
       AND wa.verb IN ('issue_edited', 'changed_status',
                       'overrode_blocked_issue_close', 'commented',
                       'comment_edited', 'added_attachment', 'linked',
                       'set_parent', 'claim_completed')
);

UPDATE issues SET status_closed = (
    COALESCE(
        (SELECT ps.category FROM project_statuses ps
          WHERE ps.project_id = issues.project_id AND ps.name = issues.status),
        CASE issues.status WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing'
             WHEN 'done' THEN 'done' ELSE 'doing' END
    ) = 'done'
);

-- The idle sort's two indexes. Every work query says `archived_at IS NULL`
-- unless it asks for archived issues, so both lead with it: an equality the
-- planner prefers to idx_issues_archived's because the rest of the key also
-- serves the ORDER BY. (A partial index on the same condition was tried and
-- lost to idx_issues_archived on a database without statistics, which is
-- every Athena database.) The first then orders by the idle key
-- (`sort:idle-desc`, `idle:` ranges); the second puts open/closed before it,
-- so `is:open sort:idle-desc` seeks the open issues and walks them in order
-- instead of walking past every closed issue, which are the most idle.
-- `id DESC` is the sort's tie-break.
CREATE INDEX idx_issues_idle
    ON issues (archived_at, COALESCE(last_work_at, created_at), id DESC);
CREATE INDEX idx_issues_closed_idle
    ON issues (archived_at, status_closed, COALESCE(last_work_at, created_at), id DESC);

-- A new work event can only move last work later: keep the larger.
CREATE TRIGGER issue_last_work_on_event
AFTER INSERT ON activity
WHEN NEW.target_kind = 'issue' AND NEW.imported_at IS NULL
 AND NEW.verb IN ('issue_edited', 'changed_status',
                  'overrode_blocked_issue_close', 'commented',
                  'comment_edited', 'added_attachment', 'linked',
                  'set_parent', 'claim_completed')
BEGIN
    UPDATE issues SET last_work_at = NEW.created_at
     WHERE id = NEW.target_id
       AND (last_work_at IS NULL OR last_work_at < NEW.created_at);
END;

-- The trail is append-only in the product, but a row can still be edited or
-- removed outside it (an operator's shell, a test back-dating history). Then
-- the old and new targets are recomputed from their trails, as the backfill
-- does, so the stored value follows the rows whatever happened to them.
CREATE TRIGGER issue_last_work_on_event_update
AFTER UPDATE OF verb, target_kind, target_id, created_at, imported_at ON activity
WHEN OLD.target_kind = 'issue' OR NEW.target_kind = 'issue'
BEGIN
    UPDATE issues SET last_work_at = (
        SELECT MAX(wa.created_at) FROM activity wa
         WHERE wa.target_kind = 'issue' AND wa.target_id = issues.id
           AND wa.imported_at IS NULL
           AND wa.verb IN ('issue_edited', 'changed_status',
                           'overrode_blocked_issue_close', 'commented',
                           'comment_edited', 'added_attachment', 'linked',
                           'set_parent', 'claim_completed')
    )
     WHERE (OLD.target_kind = 'issue' AND id = OLD.target_id)
        OR (NEW.target_kind = 'issue' AND id = NEW.target_id);
END;

CREATE TRIGGER issue_last_work_on_event_delete
AFTER DELETE ON activity
WHEN OLD.target_kind = 'issue'
BEGIN
    UPDATE issues SET last_work_at = (
        SELECT MAX(wa.created_at) FROM activity wa
         WHERE wa.target_kind = 'issue' AND wa.target_id = issues.id
           AND wa.imported_at IS NULL
           AND wa.verb IN ('issue_edited', 'changed_status',
                           'overrode_blocked_issue_close', 'commented',
                           'comment_edited', 'added_attachment', 'linked',
                           'set_parent', 'claim_completed')
    )
     WHERE id = OLD.target_id;
END;

-- Open/closed follows the issue's status and project, and the project's own
-- status rows. Each trigger recomputes from the current rows rather than
-- adjusting, so the order a multi-statement write runs in cannot matter.
CREATE TRIGGER issue_status_closed_on_insert
AFTER INSERT ON issues
BEGIN
    UPDATE issues SET status_closed = (
        COALESCE(
            (SELECT ps.category FROM project_statuses ps
              WHERE ps.project_id = issues.project_id AND ps.name = issues.status),
            CASE issues.status WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing'
                 WHEN 'done' THEN 'done' ELSE 'doing' END
        ) = 'done'
    )
     WHERE id = NEW.id;
END;

CREATE TRIGGER issue_status_closed_on_update
AFTER UPDATE OF status, project_id ON issues
BEGIN
    UPDATE issues SET status_closed = (
        COALESCE(
            (SELECT ps.category FROM project_statuses ps
              WHERE ps.project_id = issues.project_id AND ps.name = issues.status),
            CASE issues.status WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing'
                 WHEN 'done' THEN 'done' ELSE 'doing' END
        ) = 'done'
    )
     WHERE id = NEW.id;
END;

CREATE TRIGGER issue_status_closed_on_status_insert
AFTER INSERT ON project_statuses
BEGIN
    UPDATE issues SET status_closed = (
        COALESCE(
            (SELECT ps.category FROM project_statuses ps
              WHERE ps.project_id = issues.project_id AND ps.name = issues.status),
            CASE issues.status WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing'
                 WHEN 'done' THEN 'done' ELSE 'doing' END
        ) = 'done'
    )
     WHERE project_id = NEW.project_id AND status = NEW.name;
END;

CREATE TRIGGER issue_status_closed_on_status_update
AFTER UPDATE OF project_id, name, category ON project_statuses
BEGIN
    UPDATE issues SET status_closed = (
        COALESCE(
            (SELECT ps.category FROM project_statuses ps
              WHERE ps.project_id = issues.project_id AND ps.name = issues.status),
            CASE issues.status WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing'
                 WHEN 'done' THEN 'done' ELSE 'doing' END
        ) = 'done'
    )
     WHERE (project_id = OLD.project_id AND status = OLD.name)
        OR (project_id = NEW.project_id AND status = NEW.name);
END;

CREATE TRIGGER issue_status_closed_on_status_delete
AFTER DELETE ON project_statuses
BEGIN
    UPDATE issues SET status_closed = (
        COALESCE(
            (SELECT ps.category FROM project_statuses ps
              WHERE ps.project_id = issues.project_id AND ps.name = issues.status),
            CASE issues.status WHEN 'open' THEN 'todo' WHEN 'in_progress' THEN 'doing'
                 WHEN 'done' THEN 'done' ELSE 'doing' END
        ) = 'done'
    )
     WHERE project_id = OLD.project_id AND status = OLD.name;
END;
