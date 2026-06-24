-- 0015_project_keys: Jira-style per-project issue keys (e.g. ATH-12).
-- FORWARD-ONLY: once applied anywhere, never edit this file — add 0016_*.sql.
--
-- An issue's key is a project-scoped identity: a project KEY prefix (ATH) plus a
-- per-project sequence number (12). The number counts from 1 within each project
-- and is NEVER reused — moving an issue to another project assigns it a fresh
-- number there and retires the old one. To guarantee no-reuse even across deletes
-- and moves, the next number comes from a monotonic per-project counter
-- (issue_counter) that only ever increases; the issue stores the number it was
-- handed (project_seq). project_seq is NULL for an issue in no project (backlog),
-- which therefore has no key.

-- The project KEY prefix. Like spaces.key: short, the canonical human identity,
-- UNIQUE and case-insensitive so "ath" and "ATH" can't both exist. COLLATE NOCASE
-- on the column itself (not just the index) so every `WHERE key = ?` lookup is
-- case-insensitive — matching projects.name and spaces.key. SQLite cannot ADD
-- COLUMN with a UNIQUE constraint, so the uniqueness is enforced by the index
-- created at the bottom. Default '' lets the ADD COLUMN succeed on existing rows;
-- the backfill below replaces it.
ALTER TABLE projects ADD COLUMN key TEXT NOT NULL DEFAULT '' COLLATE NOCASE;

-- Monotonic high-water mark of numbers ever handed out for this project. Never
-- decremented, so a deleted or moved-away issue's number is never reissued.
ALTER TABLE projects ADD COLUMN issue_counter INTEGER NOT NULL DEFAULT 0;

-- The per-project number this issue was assigned. NULL = no project = no key.
ALTER TABLE issues ADD COLUMN project_seq INTEGER;

-- Backfill existing projects with a placeholder key derived from their id, so the
-- NOT NULL + UNIQUE invariants hold for legacy rows. 'P1', 'P2', ... are unique
-- because ids are; users can rename them afterward via project edit.
UPDATE projects SET key = 'P' || id WHERE key = '';

-- Backfill project_seq: number each project's existing issues 1..N in id order
-- (oldest issue = 1). The correlated COUNT of same-project issues with id <= this
-- one yields a stable, gap-free ordering.
UPDATE issues
   SET project_seq = (
       SELECT COUNT(*) FROM issues b
        WHERE b.project_id = issues.project_id
          AND b.id <= issues.id
   )
 WHERE project_id IS NOT NULL;

-- Set each counter to the highest number handed out (= count of its issues), so
-- the next allocation continues from there rather than colliding with a backfill.
UPDATE projects
   SET issue_counter = (
       SELECT COUNT(*) FROM issues WHERE issues.project_id = projects.id
   );

-- Enforce key uniqueness (case-insensitive) now that every row has a real key.
CREATE UNIQUE INDEX idx_projects_key ON projects(key COLLATE NOCASE);
