-- 0033_sprints: time-boxed iterations (sprints/cycles) for a project's issues.
-- FORWARD-ONLY: once this file has been applied anywhere, never edit it — add 0034.
--
-- A sprint is a named, time-boxed batch of work within ONE project (like issues, a
-- sprint belongs to a project). It moves through a small lifecycle —
-- planned → active → completed — and an issue may be IN a sprint or in the backlog
-- (issues.sprint_id NULL). The lifecycle and the "one active sprint per project"
-- rule are enforced in the data layer (aegis/sprints.py), not the schema, so the
-- table stays a plain record; state is free TEXT but only sprints.py writes it.
-- Dates are nullable 'YYYY-MM-DD' (a planned sprint may not have them yet); starting
-- and completing stamp them if unset. No ON DELETE: a sprint with issues still in it
-- can't be deleted (the issues.sprint_id FK refuses the orphan), matching projects.
CREATE TABLE sprints (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    name        TEXT    NOT NULL,
    goal        TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT 'planned',   -- planned | active | completed
    start_date  TEXT,                                 -- nullable 'YYYY-MM-DD'
    end_date    TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The hot reads are "this project's sprints" and "the project's active sprint".
CREATE INDEX idx_sprints_project ON sprints (project_id, state);

-- An issue is in at most one sprint (or the backlog). Nullable, no ON DELETE so a
-- sprint can't be dropped out from under its issues.
ALTER TABLE issues ADD COLUMN sprint_id INTEGER REFERENCES sprints(id);

-- The board/sprint read is "issues in sprint S" — index the sprint end.
CREATE INDEX idx_issues_sprint ON issues (sprint_id);
